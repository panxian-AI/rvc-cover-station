# -*- coding: utf-8 -*-
"""网易云音源（v2.2.0 从 ncm_proxy.py 里搬出来，行为不变）。

Cookie 仍然优先沿用 ncmapi/ncm_cookie.txt：扫码登录、手工粘贴、老的
设置网易云Cookie.bat 都写这一个文件，老用户升级后不用重新登录。
v2.3.0 起还能再存多个账号（cookies/netease/<id>.txt），取流时按顺序轮换。
"""
import re
import time
import urllib.parse

from .base import CookieBook, SourceError, http_get_json, try_accounts

KEY = "netease"
LABEL = "网易云音乐"
COOKIE_HINT = ("控制台点「扫码登录」最省事；也可以浏览器打开 music.163.com 登录后，"
               "F12 → Network → 任意请求 → 复制整段 Cookie（含 MUSIC_U）粘进来")
COOKIE_NAME = "netease"
STORE = CookieBook("netease", ("MUSIC_U=",), legacy_filename="ncm_cookie.txt")

BASE = "https://music.163.com"
_ANON_COOKIE = "os=pc; appver=2.9.7"


def _headers(extra=None, cookie=None):
    ck = cookie if cookie is not None else STORE.cookie()
    return {"Referer": BASE + "/", "Cookie": ck or _ANON_COOKIE, **(extra or {})}


def fetch(path, extra=None, timeout=12, cookie=None):
    """打网易云内部接口（老端点保留给 AstrBot 插件用）。"""
    return http_get_json(BASE + path, _headers(extra, cookie), timeout=timeout)


def norm_song(s):
    """补齐 ar/al/dt 与 artists/album/duration 两种格式。"""
    out = dict(s)
    ar = s.get("ar") or s.get("artists") or []
    al = s.get("al") or s.get("album") or {}
    dt = s.get("dt") or s.get("duration") or 0
    out.update({"ar": ar, "artists": ar, "al": al, "album": al,
                "dt": dt, "duration": dt, "name": s.get("name", "")})
    return out


def search(kw, limit=12, offset=0):
    q = urllib.parse.urlencode({"s": kw, "type": "1", "limit": int(limit),
                                "offset": int(offset)})
    data = fetch("/api/cloudsearch/pc?" + q)
    rows = (data.get("result") or {}).get("songs") or []
    out = []
    for s in rows[:int(limit)]:
        s = norm_song(s)
        artists = s.get("ar") or []
        out.append({
            "id": str(s.get("id")),
            "name": s.get("name") or "(无标题)",
            "artist": "/".join(a.get("name", "") for a in artists) or "未知",
            "album": (s.get("al") or {}).get("name") or "",
            "duration": int(round((s.get("dt") or 0) / 1000)),
            "album_id": str((s.get("al") or {}).get("id") or ""),
        })
    return out


def song_url(song_id):
    sid = re.sub(r"[^\d]", "", str(song_id))
    if not sid:
        raise SourceError("网易云歌曲 ID 不对：%r" % (song_id,))

    def _one(cookie):
        data = fetch("/api/song/enhance/player/url?ids=[%s]&br=320000" % sid,
                     cookie=cookie)
        rows = data.get("data") or []
        u = rows[0].get("url") if rows else ""
        if u:
            return u
        code = (rows[0].get("code") if rows else "") or data.get("code")
        raise SourceError("没给地址（code=%s）：VIP 专享 / 无版权 / 已下架" % code)

    entries = STORE.entries()
    if not entries:
        # 匿名也试一次：免费曲匿名就能拿直链，别一上来就让用户去登录
        try:
            return _one("")
        except SourceError as e:
            raise SourceError("网易云没给播放地址（%s）：这首歌是 VIP 专享、无版权或已下架。"
                              "换一首，或在控制台登录网易云后重试" % e, need_login=True)
    url, _acc = try_accounts(entries, _one)
    return url


def song_detail(ids):
    if isinstance(ids, (str, int)):
        ids = [ids]
    clean = [re.sub(r"[^\d]", "", str(x)) for x in ids]
    clean = [x for x in clean if x]
    if not clean:
        return []
    data = fetch("/api/song/detail?ids=" + urllib.parse.quote(
        "[" + ",".join(clean) + "]"))
    out = []
    for s in data.get("songs") or []:
        s = norm_song(s)
        out.append({"id": str(s.get("id")), "name": s.get("name") or "",
                    "duration": int(round((s.get("dt") or 0) / 1000))})
    return out


_LOGIN_CACHE = {"at": 0.0, "key": "", "obj": [], "one": None}


def _profile(cookie):
    data = fetch("/api/nuser/account/get", timeout=8, cookie=cookie)
    prof = data.get("profile") or {}
    return prof.get("nickname") or "", (prof.get("vipType") or 0) > 0


def accounts_status(force=False):
    """每个账号的登录态。最多真去验 PROBE_MAX 个，其余只报「已保存」。"""
    items = STORE.accounts()
    sig = tuple((x["id"], round(x["mtime"], 3)) for x in items)
    now = time.time()
    if not force and _LOGIN_CACHE["key"] == sig and _LOGIN_CACHE["at"] \
            and now - _LOGIN_CACHE["at"] < 60:
        return [dict(x) for x in _LOGIN_CACHE["obj"]]
    out, probed = [], 0
    for x in items:
        row = {"id": x["id"], "file": x["file"], "legacy": x["legacy"],
               "valid": x["valid"], "login": False, "nickname": "", "vip": False,
               "msg": ""}
        if not x["valid"]:
            row["msg"] = "文件里没有 MUSIC_U，不算登录态"
        elif probed >= STORE.PROBE_MAX:
            row.update(login=True, msg="已保存（账号多，没逐个验活）")
        else:
            probed += 1
            try:
                nick, vip = _profile(x["cookie"])
                row.update(login=bool(nick), nickname=nick, vip=vip,
                           msg="" if nick else "Cookie 没有登录态（可能已过期）")
            except Exception as e:
                # 验活失败 ≠ 已登录：网络/上游抽风时别把「没验过」说成「可用」
                row.update(login=False,
                           msg="没验成功（网络或上游异常）：%s: %s"
                               % (type(e).__name__, e))
        out.append(row)
    _LOGIN_CACHE.update(at=now, key=sig, obj=[dict(x) for x in out])
    return out


def login_status(force=False):
    """汇总成一个摘要（网关 /login/status、插件兼容用）。

    逐账号明细在 accounts_status()；这里返回「最像能用的那个账号」的信息。
    """
    accs = accounts_status(force)
    base = {"platform": KEY, "label": LABEL, "login": False, "nickname": "",
            "vip": False, "accounts": len(accs),
            "ready": sum(1 for a in accs if a["login"])}
    if not accs:
        base["msg"] = "未配置 Cookie，当前是匿名模式（VIP / 独家曲目拿不到地址）"
        return base
    ok = [a for a in accs if a["login"]]
    if not ok:
        base["msg"] = "有 %d 个 Cookie 文件，但都没登录态（可能已过期），重新扫码登录一次" % len(accs)
        return base
    base.update(login=True, nickname=ok[0]["nickname"], vip=ok[0]["vip"],
                msg="" if len(ok) == 1 else "共 %d 个账号，%d 个可用" % (len(accs), len(ok)))
    return base
