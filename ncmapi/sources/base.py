# -*- coding: utf-8 -*-
"""音源适配器的公共底座。

每个平台一个模块，对外暴露同一组名字：

    KEY            平台标识（写进 song_id 前缀、面板路由参数）
    LABEL          中文名
    COOKIE_HINT    面板上「怎么拿到 Cookie」的一句话说明
    COOKIE_NAME    cookies/<name>.txt；网易云沿用老的 ncm_cookie.txt
    MARKERS        Cookie 里必须出现的键（任一即可）才算「已保存登录态」
    search(kw, limit=12, offset=0) -> list[dict]
    song_url(song_id) -> str        失败抛 SourceError
    song_detail(ids) -> list[dict]  时长/歌名，查不到返回空表
    login_status(force=False) -> dict

设计约束（踩过的坑，别改回去）：
  · 只用标准库 —— 安装包里没有 requests，面板和 3002 都是裸解释器跑的。
  · 本机请求一律绕开系统代理（ProxyHandler({})），用户装了 Clash 之类时
    系统代理会把回环/直连截走，表现成「接口突然全 502」。
  · Cookie 写盘用 tmp + fsync + os.replace：写一半被杀不会把旧值覆盖成半个。
  · 失败要分清「没登录」和「没版权」：前者用户能自己解决（去配 Cookie），
    后者换一首歌就行。SourceError.need_login 就是给界面分派话术用的。
"""
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ncmapi/
COOKIE_DIR = os.path.join(HERE, "cookies")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def qr_opener():
    """扫码登录用的独立会话：一个 cookie jar 跟一次登录全程。

    不能用全局 OPENER：二维码申请时种的 Cookie（网易云 NMTID、QQ 的 qrsig）
    要跟着同一个会话走到底，且不同扫码窗口之间不能串。每个 QrLogin 实例一个。
    """
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPCookieProcessor(jar)), jar


def jar_cookie(jar, domain_kw):
    """从 jar 里拼一行 Cookie：只挑域名含 domain_kw 的，按名字排序保证稳定。"""
    items = []
    for c in jar:
        if c.value and (not c.domain or domain_kw in c.domain):
            items.append((c.name, c.value))
    items.sort()
    return "; ".join("%s=%s" % kv for kv in items)


class SourceError(Exception):
    """适配器统一的失败类型。

    need_login=True 表示「配了 Cookie / 换个账号就能解决」，
    界面据此提示去配置，而不是让用户怀疑站点坏了。
    """

    def __init__(self, msg, need_login=False):
        super().__init__(msg)
        self.need_login = need_login


def http_get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with OPENER.open(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url, headers=None, timeout=12):
    return _json(http_get(url, headers, timeout))


def http_post_json(url, obj, headers=None, timeout=12):
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/json", **(headers or {})})
    with OPENER.open(req, timeout=timeout) as r:
        return _json(r.read())


def _json(raw):
    try:
        return json.loads((raw or b"").decode("utf-8", "ignore"))
    except Exception:
        raise SourceError("上游返回的不是 JSON（可能被风控拦截了），稍后再试")


def norm_cookie(raw):
    """把用户粘贴的内容规整成一行 Cookie。

    兼容三种常见粘贴：整段浏览器 Cookie / `Cookie: xxx` 头 / 带多行换行。
    返回纯 cookie 串（不含注释行）。
    """
    lines = []
    for ln in (raw or "").replace("\r", "\n").split("\n"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.lower().startswith("cookie:"):
            ln = ln[7:].strip()
        lines.append(ln)
    return " ".join(" ".join(lines).split())


def _scan(path, ident, legacy, markers):
    """读一个 Cookie 文件并带上元信息（账号池与单文件存储共用）。"""
    cookie, ok = read_cookie_file(path, markers)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0.0
    return {"id": ident, "path": path, "legacy": legacy, "cookie": cookie,
            "valid": bool(ok), "mtime": mt, "file": os.path.basename(path)}


# 面板一次最多真去验几个账号：账号多了逐个探活会让「打开概览页」变成十几次请求
PROBE_MAX = 3


class CookieStore(object):
    """单文件 Cookie（v2.2.0 的形态，保留给 legacy 与老测试）。

    v2.3.0 起适配器只调 `entries()` / `accounts()` / `save()` / `remove()`，
    这个类也照着实现一份（永远只有一个 id="legacy" 的账号），
    于是「单文件」和「账号池」在适配器眼里是同一个东西。
    """

    PROBE_MAX = PROBE_MAX

    def __init__(self, name, markers, filename=None):
        self.path = os.path.join(HERE, filename) if filename \
            else os.path.join(COOKIE_DIR, name + ".txt")
        self.key = name
        self.markers = tuple(markers)
        self._lock = threading.Lock()
        self._cache = {"mtime": object(), "cookie": "", "login": False}

    @property
    def dir(self):
        return os.path.dirname(self.path)

    # ---- 读 ----
    def _read(self):
        return read_cookie_file(self.path, self.markers)

    def state(self):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            mt = None
        with self._lock:
            if self._cache["mtime"] != mt:
                cookie, login = self._read()
                self._cache.update(mtime=mt, cookie=cookie, login=login)
            return dict(self._cache)

    def cookie(self):
        return self.state()["cookie"]

    def logged_in(self):
        return self.state()["login"]

    # ---- 账号接口（与 CookieBook 同形）----
    def accounts(self):
        if not os.path.isfile(self.path):
            return []
        return [_scan(self.path, "legacy", True, self.markers)]

    def entries(self):
        return [(x["id"], x["cookie"]) for x in self.accounts() if x["valid"]]

    def get(self, ident):
        return next((x for x in self.accounts() if x["id"] == ident), None)

    # ---- 写 ----
    def save(self, raw, require=None, ident=None):
        marks = tuple(require or self.markers)
        cookie = _check_cookie(raw, marks)
        write_cookie_file(self.path, cookie, marks)
        with self._lock:
            self._cache["mtime"] = object()
        return {"id": "legacy", "path": self.path,
                "file": os.path.basename(self.path)}

    def remove(self, ident=None):
        return self.clear()

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return False
        with self._lock:
            self._cache["mtime"] = object()
        return True


def read_cookie_file(path, markers):
    """读一份 Cookie 文件 → (cookie, 有效)。

    文件不存在 / 读不了都按「没有」处理；有效 = 含任一标记键。
    标记键是判断「这是登录态而不是一堆广告 cookie」的唯一依据。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except (FileNotFoundError, OSError):
        return "", False
    cookie = norm_cookie(raw)
    login = bool(cookie) and any(m in cookie for m in markers)
    return (cookie if login else ""), login


def write_cookie_file(path, cookie, markers):
    """原子写一份 Cookie 文件（tmp + fsync + os.replace）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = "# 由控制台保存（%s）；改完 3002 会自动重读，不用重启\n%s\n" % (
        ", ".join(markers), cookie)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _check_cookie(raw, markers):
    """规整 + 校验标记键；不合格直接抛 ValueError（调用方回 400，不落盘）。"""
    cookie = norm_cookie(raw)
    if not cookie:
        raise ValueError("没有可保存的内容")
    if not any(m in cookie for m in markers):
        raise ValueError("这段 Cookie 里没有 %s —— 确认是在已登录的页面上复制的"
                         % " / ".join(markers))
    return cookie


_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


def safe_id(text, limit=32):
    """账号标识只留安全字符：它要当文件名，绝不能带路径分隔符。"""
    s = _SAFE_ID.sub("", str(text or ""))[:limit]
    return s or "account"


class CookieBook(object):
    """一个平台可以存**多个账号**的 Cookie（v2.3.0）。

    为什么要多个：同一平台不同账号的版权 / 会员不一样。一首歌这个号拿不到
    播放地址，换另一个号往往就拿到了；搜索用哪个号都一样。

    存放位置（三处都读，只有池里会写）：
        ncmapi/ncm_cookie.txt           网易云扫码 / 老窗口写的（legacy，v2.1.x）
        ncmapi/cookies/<key>.txt        v2.2.0 的单文件（legacy，上一版）
        ncmapi/cookies/<key>/<id>.txt   账号池（v2.3.0 起）

    legacy 条目排在前面、id 固定 "legacy"，界面显示成「默认账号」；
    删掉它等于退出那次登录（网易云就是删 ncm_cookie.txt），与以前行为一致。
    """

    PROBE_MAX = PROBE_MAX

    def __init__(self, name, markers, legacy_filename=None, ident_fn=None):
        self.key = name
        self.markers = tuple(markers)
        self.dir = os.path.join(COOKIE_DIR, name)
        self.ident_fn = ident_fn
        legacy = []
        if legacy_filename:
            legacy.append(os.path.join(HERE, legacy_filename))
        legacy.append(os.path.join(COOKIE_DIR, name + ".txt"))
        self.legacy_paths = legacy
        self._lock = threading.Lock()
        self._cache = {"sig": object(), "items": []}

    # ---- 扫描（按签名缓存，别每次开页都读十几个文件）----
    def _sig(self):
        parts = []
        for p in self.legacy_paths:
            try:
                parts.append((os.path.basename(p), os.path.getmtime(p)))
            except OSError:
                pass
        try:
            for fn in sorted(os.listdir(self.dir)):
                if fn.lower().endswith(".txt"):
                    try:
                        parts.append((fn, os.path.getmtime(
                            os.path.join(self.dir, fn))))
                    except OSError:
                        pass
        except OSError:
            pass
        return tuple(parts)

    def _scan_one(self, path, ident, legacy):
        return _scan(path, ident, legacy, self.markers)

    def accounts(self):
        """全部的条目（含无效的），legacy 优先、其后按修改时间新→旧。"""
        sig = self._sig()
        with self._lock:
            if self._cache["sig"] == sig:
                return [dict(x) for x in self._cache["items"]]
        items = []
        for p in self.legacy_paths:
            if os.path.isfile(p):
                items.append(self._scan_one(p, "legacy", True))
        try:
            names = [fn for fn in os.listdir(self.dir) if fn.lower().endswith(".txt")]
        except OSError:
            names = []
        for fn in names:
            items.append(self._scan_one(os.path.join(self.dir, fn),
                                        os.path.splitext(fn)[0], False))
        items.sort(key=lambda x: (0 if x["legacy"] else 1, -x["mtime"]))
        with self._lock:
            self._cache.update(sig=sig, items=[dict(x) for x in items])
        return items

    def entries(self):
        """能用的账号 → [(id, cookie)]，顺序就是取流时的尝试顺序。"""
        return [(x["id"], x["cookie"]) for x in self.accounts() if x["valid"]]

    def get(self, ident):
        for x in self.accounts():
            if x["id"] == ident:
                return x
        return None

    # ---- 兼容 v2.2.0 的单账号接口（适配器与老测试都按这几个名字调）----
    def state(self):
        acc = next((x for x in self.accounts() if x["valid"]), None)
        return {"cookie": acc["cookie"] if acc else "",
                "login": bool(acc), "id": acc["id"] if acc else ""}

    def cookie(self):
        return self.state()["cookie"]

    def logged_in(self):
        return bool(self.state()["login"])

    # ---- 写 ----
    def save(self, raw, require=None, ident=None):
        """存一个账号；同一账号再存会覆盖（重登不产生重复条目）。"""
        marks = tuple(require or self.markers)
        cookie = _check_cookie(raw, marks)
        acc_id = safe_id(ident or self._guess_ident(cookie))
        path = os.path.join(self.dir, acc_id + ".txt")
        write_cookie_file(path, cookie, marks)
        with self._lock:
            self._cache["sig"] = object()
        return {"id": acc_id, "path": path, "file": os.path.basename(path)}

    def _guess_ident(self, cookie):
        if self.ident_fn:
            try:
                got = self.ident_fn(cookie)
                if got:
                    return safe_id(got)
            except Exception:
                pass
        m = re.search(r"(?:^|;\s*)(?:uin|userid)=o?(\d{3,20})", cookie)
        if m:
            return "u" + m.group(1)
        m = re.search(r"MUSIC_U=([0-9A-Za-z]{6})", cookie)
        if m:
            return "n" + m.group(1)
        import hashlib
        return "h" + hashlib.md5(cookie.encode("utf-8")).hexdigest()[:10]

    def remove(self, ident):
        """删一个账号（legacy 就是删那个老文件）。返回是否真删掉了。"""
        ident = str(ident or "").strip()
        if ident == "legacy":
            gone = False
            for p in self.legacy_paths:
                try:
                    os.remove(p)
                    gone = True
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
            with self._lock:
                self._cache["sig"] = object()
            return gone
        if not ident:
            return False
        path = os.path.join(self.dir, safe_id(ident) + ".txt")
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        with self._lock:
            self._cache["sig"] = object()
        return True

    def clear(self):
        """删掉这个平台的**所有**账号，返回删了几个。"""
        n = 0
        for p in self.legacy_paths:
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
        try:
            for fn in os.listdir(self.dir):
                if fn.lower().endswith(".txt"):
                    try:
                        os.remove(os.path.join(self.dir, fn))
                        n += 1
                    except OSError:
                        pass
        except OSError:
            pass
        with self._lock:
            self._cache["sig"] = object()
        return n


def try_accounts(entries, fn, what="取流"):
    """按顺序用每个账号试 fn(cookie)，返回第一个非空结果。

    entries 为空 = 一个账号都没配（调用方自己决定要不要提示「去配 Cookie」）。
    全失败时把每个账号的原因拼起来抛出去 —— 只报「失败了」等于没说。
    """
    if not entries:
        raise SourceError("没有可用的 %s 账号" % what, need_login=True)
    errs = []
    for acc_id, cookie in entries:
        try:
            got = fn(cookie)
            if got:
                return got, acc_id
            errs.append("%s：没返回地址" % acc_id)
        except SourceError as e:
            errs.append("%s：%s" % (acc_id, e))
        except Exception as e:
            errs.append("%s：%s: %s" % (acc_id, type(e).__name__, e))
    raise SourceError("试过 %d 个账号都没拿到 —— %s"
                      % (len(entries), "；".join(errs[:3])))


def split_id(raw):
    """`qq-0035GveV3i9dBM` / `kugou:ABC` / `netease-123` / `123` → (平台, 纯 id)。

    不带前缀一律当网易云 —— 老队列、老成品索引、AstrBot 插件传的都是纯数字。
    """
    s = str(raw or "").strip()
    m = re.match(r"^(netease|qq|kugou)[:\-]([A-Za-z0-9]{1,40})$", s, re.I)
    if m:
        return m.group(1).lower(), m.group(2)
    return "netease", s


def make_id(platform, song_id):
    """统一成 `<平台>-<id>`；网易云保持纯数字（老数据零迁移）。"""
    song_id = str(song_id or "").strip()
    return song_id if platform == "netease" else "%s-%s" % (platform, song_id)
