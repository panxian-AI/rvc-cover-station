# -*- coding: utf-8 -*-
"""QQ 音乐音源。

搜索（c.y.qq.com/soso）匿名就能用；**取播放地址必须有登录 Cookie**
（Cookie 里的 uin + qm_keyst），否则 vkey 接口只回 empty purl —— 这不是
「接口坏了」，是 QQ 音乐从 2023 年起就不再给匿名发完整直链了。所以这一路
失败时明确提示「去配 Cookie」，而不是含糊地说换一首。

Cookie 获取：浏览器打开 y.qq.com 登录 → F12 → Network → 任意请求 →
复制整段 Cookie（关键项 uin、qm_keyst）。只填这两项也能用。
"""
import re
import time

from .base import CookieBook, SourceError, http_get_json, http_post_json, try_accounts

KEY = "qq"
LABEL = "QQ 音乐"
COOKIE_HINT = ("浏览器打开 y.qq.com 登录 → F12 → Network → 任意请求 → 复制整段 Cookie"
               "（关键项：uin、qm_keyst），粘进来保存即可；可以存多个账号轮流试")
COOKIE_NAME = "qq"
STORE = CookieBook("qq", ("qm_keyst=", "qqmusic_key="))

SEARCH_URL = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
              "?format=json&p=%d&n=%d&w=%s&cr=1&new_json=1")
MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
DETAIL_URL = ("https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
              "?songmid=%s&format=json&platform=yqq")
GUID = "1000000000"          # 官方客户端用的就是 10 位；固定值即可，不参与鉴权

# 登录态自检用的一首免费曲（小苹果）。有完整 purl = Cookie 真能取流；
# 不要拿 VIP 曲当探针，否则「会员过期」会被误判成「Cookie 失效」。
PROBE_MID = "0035GveV3i9dBM"


def _headers(cookie=None):
    ck = cookie if cookie is not None else STORE.cookie()
    return {"Referer": "https://y.qq.com/", "Cookie": ck}


def uin(cookie=None):
    """从 Cookie 里取数字 uin（浏览器常写成 uin=o1234567890）。"""
    c = cookie if cookie is not None else STORE.cookie()
    m = re.search(r"(?:^|;\s*)(?:uin|wxuin)=o?(\d+)", c or "")
    return m.group(1) if m else "0"


def search(kw, limit=12, offset=0):
    import urllib.parse
    page = int(offset) // max(1, int(limit)) + 1
    url = SEARCH_URL % (page, int(limit), urllib.parse.quote(str(kw)))
    d = http_get_json(url, _headers())
    rows = (((d.get("data") or {}).get("song") or {}).get("list")) or []
    out = []
    for s in rows[:int(limit)]:
        # new_json=1 用 mid/title/album 对象；老接口用 songmid/songname/albumname。
        # 两套都认，接口哪天关掉其中一个也不至于整页搜不出东西。
        mid = s.get("mid") or s.get("songmid") or ""
        album = s.get("album") if isinstance(s.get("album"), dict) else {}
        singers = s.get("singer") or []
        pay = s.get("pay") if isinstance(s.get("pay"), dict) else {}
        out.append({
            "id": mid,
            "name": s.get("title") or s.get("songname") or s.get("name") or "(无标题)",
            "artist": "/".join(x.get("name") or x.get("title") or ""
                               for x in singers) or "未知",
            "album": album.get("name") or s.get("albumname") or "",
            "duration": int(s.get("interval") or 0),
            "album_id": str(album.get("id") or s.get("albumid") or ""),
            "paid": bool(pay.get("pay_play") or pay.get("pay_down")),
        })
    return [x for x in out if x["id"]]


def _vkey(mid, cookie=None):
    u = uin(cookie)
    body = {
        "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                  "param": {"guid": GUID, "songmid": [mid], "songtype": [0],
                            "uin": u, "loginflag": 1, "platform": "20"}},
        "comm": {"uin": int(u) if u.isdigit() else 0, "format": "json",
                 "ct": 24, "cv": 0},
    }
    d = http_post_json(MUSICU_URL, body, _headers(cookie))
    data = (d.get("req_0") or {}).get("data") or {}
    info = (data.get("midurlinfo") or [{}])[0]
    purl = info.get("purl") or ""
    sip = (data.get("sip") or [""])[0]
    if purl and sip:
        return sip + purl
    return ""


def song_url(song_id):
    mid = str(song_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,20}", mid):
        raise SourceError("QQ 音乐歌曲 ID 不对：%r" % (song_id,))
    entries = STORE.entries()
    if entries:
        try:
            url, _acc = try_accounts(entries, lambda ck: _vkey(mid, ck))
            return url
        except SourceError as e:
            raise SourceError("QQ 音乐没给这首歌的直链：多为 VIP / 数字专辑曲目"
                              "（换一个账号往往就有了）—— %s" % e)
    # 一个账号都没配：先按匿名试一次（上游现在基本只回试听，留着是为了兼容
    # 以后放开的可能），拿不到再明确让用户去配 Cookie
    url = _vkey(mid, "")
    if url:
        return url
    raise SourceError("QQ 音乐需要登录 Cookie 才能拿到完整音源（匿名只给试听，"
                      "拿它翻唱会得到半首歌）。去控制台「音源平台」粘贴 y.qq.com "
                      "的 Cookie 即可", need_login=True)


def song_detail(ids):
    if isinstance(ids, (str, int)):
        ids = [ids]
    out = []
    for mid in ids:
        mid = str(mid).strip()
        if not mid:
            continue
        try:
            d = http_get_json(DETAIL_URL % mid, {"Referer": "https://y.qq.com/"})
            row = (d.get("data") or [{}])[0]
            out.append({"id": mid, "name": row.get("name") or "",
                        "duration": int(row.get("interval") or 0)})
        except Exception:
            continue
    return out


_LOGIN_CACHE = {"at": 0.0, "key": "", "obj": [], "one": None}


def accounts_status(force=False):
    """逐个账号拿一首免费曲试 vkey：有 purl = 这个 Cookie 真能取流。"""
    items = STORE.accounts()
    sig = tuple((x["id"], round(x["mtime"], 3)) for x in items)
    now = time.time()
    if not force and _LOGIN_CACHE["key"] == sig and _LOGIN_CACHE["at"] \
            and now - _LOGIN_CACHE["at"] < 60:
        return [dict(x) for x in _LOGIN_CACHE["obj"]]
    out, probed = [], 0
    for x in items:
        u = uin(x["cookie"])
        row = {"id": x["id"], "file": x["file"], "legacy": x["legacy"],
               "valid": x["valid"], "login": False, "nickname":
               ("QQ " + u) if u != "0" else "", "vip": False, "msg": ""}
        if not x["valid"]:
            row["msg"] = "文件里没有 qm_keyst，不算登录态"
        elif probed >= STORE.PROBE_MAX:
            row.update(login=True, msg="已保存（账号多，没逐个验活）")
        else:
            probed += 1
            try:
                ok = bool(_vkey(PROBE_MID, x["cookie"]))
                row.update(login=ok, msg="✅ 有效，能取完整音源" if ok
                           else "取不到直链：可能已过期，重新登录 y.qq.com 复制一次")
            except Exception as e:
                # 同上：没验成功就别显示成可用
                row.update(login=False,
                           msg="没验成功（网络或上游异常）：%s: %s"
                               % (type(e).__name__, e))
        out.append(row)
    _LOGIN_CACHE.update(at=now, key=sig, obj=[dict(x) for x in out])
    return out


def login_status(force=False):
    accs = accounts_status(force)
    base = {"platform": KEY, "label": LABEL, "login": False, "nickname": "",
            "vip": False, "accounts": len(accs),
            "ready": sum(1 for a in accs if a["login"])}
    if not accs:
        base["msg"] = "未配置 Cookie：能搜索，但拿不到播放地址（QQ 音乐不再给匿名发直链）"
        return base
    ok = [a for a in accs if a["login"]]
    if not ok:
        base["msg"] = "有 %d 个 Cookie，但都取不到直链（可能都已过期）" % len(accs)
        return base
    base.update(login=True, nickname=ok[0]["nickname"],
                msg="" if len(ok) == 1 else "共 %d 个账号，%d 个可用" % (len(accs), len(ok)))
    return base


# ---------------- 扫码登录（v2.4.0） ----------------
# 走 QQ 互联的网页扫码（ptlogin2）：手机 QQ 右上角「+」→ 扫一扫 → 确认。
# 二维码由腾讯现画（PNG 很小，直接转 base64 内联，内容我们没法自己编）；
# 登录成功后跟随 check_sig 跳转，y.qq.com 会下发 qm_keyst —— 那就是取流要的登录态。

PT_APPID = "716027609"      # y.qq.com 网页版
PT_DAID = "383"
PT_3RD = "100497308"
PT_SHOW = ("https://ptlogin2.qq.com/ptqrshow?appid=716027609&e=2&l=M&s=3&d=72"
           "&v=4&t={t}&daid=383&pt_3rd_aid=100497308")
PT_LOGIN = ("https://ptlogin2.qq.com/ptqrlogin?u1=https%3A%2F%2Fgraph.qq.com"
            "%2Foauth2.0%2Flogin_jump&ptqrtoken={tok}&ptredirect=0&h=1&t=1"
            "&g=1&from_ui=1&ptlang=2052&action=0-0-{ts}&js_ver=20102616"
            "&js_type=1&login_sig=&pt_uistyle=40&aid=716027609&daid=383"
            "&pt_3rd_aid=100497308&has_u1sso=1&uin=")


def hash33(s):
    """qrsig → ptqrtoken（腾讯的 hash33，社区通用实现）。"""
    e = 0
    for ch in str(s or ""):
        e += (e << 5) + ord(ch)
    return 2147483647 & e


def gtk_of(p_skey):
    """p_skey → g_tk（初值 5381 的 hash33，QQ 互联授权要用）。"""
    e = 5381
    for ch in str(p_skey or ""):
        e += (e << 5) + ord(ch)
    return 2147483647 & e


def _guid():
    import random
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


class QrLogin(object):
    """QQ 扫码：ptqrshow 出图 + qrsig → 轮询 ptqrlogin → 跟 check_sig 收 Cookie。

    状态映射成网易云那套码，前端一份逻辑通吃：
    66 → 801 等待；67 → 800 过期；0 → 803 成功（跟 check_sig）。
    QQ 没有「已扫码待确认」的中间态，扫完确认前一直是 66。
    """

    def __init__(self):
        from .base import qr_opener
        self.opener, self.jar = qr_opener()

    def _get(self, url, referer, timeout=12):
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_QQ, "Referer": referer})
        with self.opener.open(req, timeout=timeout) as r:
            return r.read()

    def start(self):
        import base64
        png = self._get(PT_SHOW.format(t=_now_ms()),
                        "https://xui.ptlogin2.qq.com/")
        if not png.startswith(b"\x89PNG"):
            raise SourceError("腾讯没返回二维码图片（返回 %d 字节）" % len(png))
        qrsig = ""
        for c in self.jar:
            if c.name == "qrsig" and c.value:
                qrsig = c.value
                break
        if not qrsig:
            raise SourceError("腾讯没种 qrsig，扫码会话建立不起来")
        self._key = qrsig
        return {"key": qrsig,
                "img": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}

    def poll(self):
        if not getattr(self, "_key", ""):
            raise SourceError("还没申请二维码")
        text = self._get(PT_LOGIN.format(tok=hash33(self._key), ts=_now_ms()),
                         "https://xui.ptlogin2.qq.com/").decode("utf-8", "ignore")
        m = re.match(r"ptuiCB\('(\d+)','[^']*','([^']*)'", text)
        if not m:
            raise SourceError("腾讯返回的不是 ptuiCB：%r" % text[:80])
        code, jump = int(m.group(1)), m.group(2)
        if code == 0:
            if not jump:
                raise SourceError("腾讯说登录成功但没给跳转地址")
            return 803, self._finish(jump)
        if code == 67:
            return 800, ""
        return 801, ""

    # ---- 登录成功后的收尾（照 y.qq.com 官网页面自己的顺序）----
    # ① 跟 check_sig：种下 p_skey（.qq.com），用它的值算 g_tk；
    # ② POST oauth2.0/authorize（已登录自动授权）→ 跳转地址里拿 code；
    # ③ POST musicu.fcg 的 QQConnectLogin.LoginServer 用 code 换登录态，
    #    qm_keyst 就在这一步的 Set-Cookie 里。少拿一步都取不了流。
    def _finish(self, check_sig_url):
        import json as _json
        import urllib.parse
        import urllib.request
        self._get(check_sig_url, "https://xui.ptlogin2.qq.com/", timeout=15)
        p_skey = self._jar_get("p_skey")
        gtk = gtk_of(p_skey) if p_skey else 0
        form = urllib.parse.urlencode({
            "response_type": "code", "client_id": PT_3RD,
            "redirect_uri": ("https://y.qq.com/portal/wx_redirect.html"
                             "?login_type=1&surl=https://y.qq.com/"),
            "scope": "get_user_info,get_app_friends", "state": "state",
            "switch": "", "from_ptlogin": "1", "src": "1",
            "update_auth": "1", "openapi": "1010_1030", "g_tk": gtk,
            "auth_time": _now_ms(), "ui": _guid(),
        }).encode()
        req = urllib.request.Request("https://graph.qq.com/oauth2.0/authorize",
                                     data=form, headers={
            "User-Agent": UA_QQ, "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://graph.qq.com/oauth2.0/show?which=Login"})
        code = ""
        try:
            with self.opener.open(req, timeout=15) as r:
                final = r.geturl()
            m = re.search(r"[?&]code=([A-Za-z0-9]+)", final)
            if m:
                code = m.group(1)
        except Exception:
            pass
        if not code:
            raise SourceError("QQ 授权没拿到 code（互联那边可能要求网页确认），"
                              "请改用粘贴 Cookie")
        body = _json.dumps({
            "comm": {"g_tk": gtk, "platform": "yqq", "ct": 24, "cv": 0},
            "req": {"module": "QQConnectLogin.LoginServer", "method": "QQLogin",
                    "param": {"code": code}}}).encode()
        req = urllib.request.Request("https://u.y.qq.com/cgi-bin/musicu.fcg",
                                     data=body, headers={
            "User-Agent": UA_QQ, "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://y.qq.com/"})
        with self.opener.open(req, timeout=15) as r:
            r.read()
        from .base import jar_cookie
        cookie = jar_cookie(self.jar, "qq.com")
        return cookie if "qm_keyst=" in cookie else ""

    def _jar_get(self, name):
        for c in self.jar:
            if c.name == name and c.value:
                return c.value
        return ""


def _now_ms():
    import time
    return int(time.time() * 1000)


UA_QQ = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
