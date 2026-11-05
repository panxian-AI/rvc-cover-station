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
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ncmapi/
COOKIE_DIR = os.path.join(HERE, "cookies")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


class CookieStore(object):
    """一个平台一份 Cookie 文件；按 mtime 热重载（改完不用重启 3002）。"""

    def __init__(self, name, markers, filename=None):
        self.path = os.path.join(HERE, filename) if filename \
            else os.path.join(COOKIE_DIR, name + ".txt")
        self.markers = tuple(markers)
        self._lock = threading.Lock()
        self._cache = {"mtime": object(), "cookie": "", "login": False}

    # ---- 读 ----
    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        except (FileNotFoundError, OSError):
            return "", False
        cookie = norm_cookie(raw)
        login = bool(cookie) and any(m in cookie for m in self.markers)
        return (cookie if login else ""), login

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

    # ---- 写 ----
    def save(self, raw, require=None):
        cookie = norm_cookie(raw)
        if not cookie:
            raise ValueError("没有可保存的内容")
        marks = tuple(require or self.markers)
        if not any(m in cookie for m in marks):
            raise ValueError("这段 Cookie 里没有 %s —— 确认是在已登录的页面上复制的"
                             % " / ".join(marks))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        text = "# 由控制台保存（%s）；改完 3002 会自动重读，不用重启\n%s\n" % (
            ", ".join(marks), cookie)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        with self._lock:
            self._cache["mtime"] = object()      # 强制下次重读
        return self.path

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return False
        with self._lock:
            self._cache["mtime"] = object()
        return True


def split_id(raw):
    """`qq-0035GveV3i9dBM` / `kugou:ABC` / `netease-123` / `123` → (平台, 纯 id)。

    不带前缀一律当网易云 —— 老队列、老成品索引、AstrBot 插件传的都是纯数字。
    """
    s = str(raw or "").strip()
    import re as _re
    m = _re.match(r"^(netease|qq|kugou)[:\-]([A-Za-z0-9]{1,40})$", s, _re.I)
    if m:
        return m.group(1).lower(), m.group(2)
    return "netease", s


def make_id(platform, song_id):
    """统一成 `<平台>-<id>`；网易云保持纯数字（老数据零迁移）。"""
    song_id = str(song_id or "").strip()
    return song_id if platform == "netease" else "%s-%s" % (platform, song_id)
