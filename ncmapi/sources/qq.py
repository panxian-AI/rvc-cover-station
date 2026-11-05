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

from .base import CookieStore, SourceError, http_get_json, http_post_json

KEY = "qq"
LABEL = "QQ 音乐"
COOKIE_HINT = ("浏览器打开 y.qq.com 登录 → F12 → Network → 任意请求 → 复制整段 Cookie"
               "（关键项：uin、qm_keyst），粘进来保存即可")
COOKIE_NAME = "qq"
STORE = CookieStore("qq", ("qm_keyst=", "qqmusic_key="))

SEARCH_URL = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
              "?format=json&p=%d&n=%d&w=%s&cr=1&new_json=1")
MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
DETAIL_URL = ("https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
              "?songmid=%s&format=json&platform=yqq")
GUID = "1000000000"          # 官方客户端用的就是 10 位；固定值即可，不参与鉴权

# 登录态自检用的一首免费曲（小苹果）。有完整 purl = Cookie 真能取流；
# 不要拿 VIP 曲当探针，否则「会员过期」会被误判成「Cookie 失效」。
PROBE_MID = "0035GveV3i9dBM"


def _headers():
    return {"Referer": "https://y.qq.com/", "Cookie": STORE.cookie()}


def uin():
    """从 Cookie 里取数字 uin（浏览器常写成 uin=o1234567890）。"""
    c = STORE.cookie()
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


def _vkey(mid):
    u = uin()
    body = {
        "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                  "param": {"guid": GUID, "songmid": [mid], "songtype": [0],
                            "uin": u, "loginflag": 1, "platform": "20"}},
        "comm": {"uin": int(u) if u.isdigit() else 0, "format": "json",
                 "ct": 24, "cv": 0},
    }
    d = http_post_json(MUSICU_URL, body, _headers())
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
    url = _vkey(mid)
    if url:
        return url
    if not STORE.logged_in():
        raise SourceError("QQ 音乐需要登录 Cookie 才能拿到完整音源（匿名只给试听，"
                          "拿它翻唱会得到半首歌）。去控制台「音源平台」粘贴 y.qq.com "
                          "的 Cookie 即可", need_login=True)
    raise SourceError("QQ 音乐没给这首歌的直链：多为 VIP / 数字专辑曲目，"
                      "或 Cookie 已过期（重新登录 y.qq.com 复制一次）")


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


_LOGIN_CACHE = {"at": 0.0, "obj": None}


def login_status(force=False):
    base = {"platform": KEY, "label": LABEL, "login": False, "nickname": "",
            "vip": False}
    if not STORE.logged_in():
        base["msg"] = "未配置 Cookie：能搜索，但拿不到播放地址（QQ 音乐不再给匿名发直链）"
        return base
    now = time.time()
    if not force and _LOGIN_CACHE["obj"] and now - _LOGIN_CACHE["at"] < 60:
        return _LOGIN_CACHE["obj"]
    try:
        ok = bool(_vkey(PROBE_MID))
        obj = dict(base, login=ok,
                   msg="✅ Cookie 有效，可以获取完整音源" if ok else
                       "Cookie 已保存但取不到直链：可能已过期，重新登录 y.qq.com 复制一次")
    except Exception as e:
        obj = dict(base, login=False, msg="检测失败：%s: %s" % (type(e).__name__, e))
    _LOGIN_CACHE.update(at=now, obj=obj)
    return obj
