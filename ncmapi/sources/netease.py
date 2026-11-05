# -*- coding: utf-8 -*-
"""网易云音源（v2.2.0 从 ncm_proxy.py 里搬出来，行为不变）。

Cookie 仍然沿用 ncmapi/ncm_cookie.txt：扫码登录、手工粘贴、老的
设置网易云Cookie.bat 都写这一个文件，老用户升级后不用重新登录。
"""
import re
import time
import urllib.parse

from .base import CookieStore, SourceError, http_get_json

KEY = "netease"
LABEL = "网易云音乐"
COOKIE_HINT = ("控制台点「扫码登录」最省事；也可以浏览器打开 music.163.com 登录后，"
               "F12 → Network → 任意请求 → 复制整段 Cookie（含 MUSIC_U）粘进来")
COOKIE_NAME = "netease"
STORE = CookieStore("netease", ("MUSIC_U=",), filename="ncm_cookie.txt")

BASE = "https://music.163.com"
_ANON_COOKIE = "os=pc; appver=2.9.7"


def _headers(extra=None):
    st = STORE.state()
    cookie = st["cookie"] if st["login"] else _ANON_COOKIE
    return {"Referer": BASE + "/", "Cookie": cookie, **(extra or {})}


def fetch(path, extra=None, timeout=12):
    """打网易云内部接口（老端点保留给 AstrBot 插件用）。"""
    return http_get_json(BASE + path, _headers(extra), timeout=timeout)


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
    data = fetch("/api/song/enhance/player/url?ids=[%s]&br=320000" % sid)
    rows = data.get("data") or []
    u = rows[0].get("url") if rows else ""
    if u:
        return u
    code = (rows[0].get("code") if rows else "") or data.get("code")
    raise SourceError("网易云没给播放地址（code=%s）：这首歌是 VIP 专享、无版权或已下架。"
                      "换一首，或在控制台登录网易云后重试" % code,
                      need_login=not STORE.logged_in())


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


_LOGIN_CACHE = {"at": 0.0, "obj": None}


def login_status(force=False):
    """返回 {platform, key, label, login, nickname, vip, msg}。缓存 60 秒。"""
    st = STORE.state()
    base = {"platform": KEY, "label": LABEL, "login": False, "nickname": "",
            "vip": False}
    if not st["login"]:
        base["msg"] = "未配置 Cookie，当前是匿名模式（VIP / 独家曲目拿不到地址）"
        return base
    now = time.time()
    if not force and _LOGIN_CACHE["obj"] and now - _LOGIN_CACHE["at"] < 60:
        return _LOGIN_CACHE["obj"]
    try:
        prof = (fetch("/api/nuser/account/get", timeout=8).get("profile") or {})
        nick = prof.get("nickname") or ""
        obj = dict(base, login=bool(nick), nickname=nick,
                   vip=(prof.get("vipType") or 0) > 0,
                   msg="" if nick else "Cookie 没有登录态（可能已过期），重新扫码登录一次")
    except Exception as e:
        obj = dict(base, login=st["login"],
                   msg="查询失败：%s: %s" % (type(e).__name__, e))
    _LOGIN_CACHE.update(at=now, obj=obj)
    return obj
