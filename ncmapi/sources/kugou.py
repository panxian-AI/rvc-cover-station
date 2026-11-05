# -*- coding: utf-8 -*-
"""酷狗音乐音源。

搜索走 songsearch.kugou.com（匿名可用）；取流走 trackercdn 的 v2 接口：
    key = md5(hash + "kgcloudv2")
    status=1 有 url（免费/普通音质）；status=2 表示该曲要会员或付费。
所以免费曲不配 Cookie 也能翻唱，付费曲才需要登录 Cookie —— 这一点和 QQ 相反。

Cookie 获取：浏览器打开 kugou.com 登录 → F12 → Network → 任意请求 →
复制整段 Cookie（关键项 token、userid）。
"""
import hashlib
import time
import urllib.parse

from .base import CookieStore, SourceError, http_get, http_get_json

KEY = "kugou"
LABEL = "酷狗音乐"
COOKIE_HINT = ("浏览器打开 kugou.com 登录 → F12 → Network → 任意请求 → 复制整段 Cookie"
               "（关键项：token、userid）。免费曲不配也能翻唱，付费曲才需要")
COOKIE_NAME = "kugou"
STORE = CookieStore("kugou", ("token=", "userid="))

SEARCH_URL = ("https://songsearch.kugou.com/song_search_v2?keyword=%s&page=%d"
              "&pagesize=%d&platform=WebFilter&userid=0&clientver=2000"
              "&iscorrection=1&privilege_filter=0")
TRACKER_URL = ("https://trackercdn.kugou.com/i/v2/?key=%s&hash=%s&br=%s"
               "&appid=1005&pid=2&cmd=25&behavior=play")
DETAIL_URL = "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=%s"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
             "Mobile/15E148 Safari/604.1")
# 从高到低试；hq 没有就退 320k/128k，避免「只有标准音质」的歌直接失败
BRS = ("hq", "320k", "128k")


def _headers():
    return {"Referer": "https://www.kugou.com/", "Cookie": STORE.cookie()}


def tracker_key(file_hash):
    """trackercdn v2 的 key 算法（hash 用大写原样拼 kgcloudv2 再 md5）。"""
    return hashlib.md5((str(file_hash).upper() + "kgcloudv2").encode()).hexdigest()


def search(kw, limit=12, offset=0):
    page = int(offset) // max(1, int(limit)) + 1
    url = SEARCH_URL % (urllib.parse.quote(str(kw)), page, int(limit))
    d = http_get_json(url, _headers())
    rows = (d.get("data") or {}).get("lists") or []
    out = []
    for s in rows[:int(limit)]:
        h = (s.get("FileHash") or s.get("Hash") or "").upper()
        if not h:
            continue
        name = (s.get("SongName") or "").replace("<em>", "").replace("</em>", "")
        artist = (s.get("SingerName") or "").replace("<em>", "").replace("</em>", "")
        out.append({
            "id": h,
            "name": name or "(无标题)",
            "artist": artist or "未知",
            "album": s.get("AlbumName") or "",
            "duration": int(s.get("Duration") or 0),
            "album_id": str(s.get("AlbumID") or ""),
            "paid": int(s.get("Privilege") or 0) != 0,
        })
    return out


def song_url(song_id):
    h = str(song_id or "").strip().upper()
    if not h or not h.isalnum():
        raise SourceError("酷狗歌曲 ID（hash）不对：%r" % (song_id,))
    key = tracker_key(h)
    status = None
    for br in BRS:
        d = http_get_json(TRACKER_URL % (key, h, br), _headers())
        status = d.get("status")
        urls = d.get("url") or []
        if status == 1 and urls:
            return urls[0]
        if status == 0:
            # key 算错 / 接口改了 —— 换音质也一样，直接报出来，别静默重试
            raise SourceError("酷狗取流被拒（Bad key），可能是接口改了，"
                              "请在 GitHub 反馈：%s" % (d.get("error") or ""))
    if status == 2:
        raise SourceError("酷狗这首歌需要会员 / 付费才能取流"
                          + ("；已保存的 Cookie 可能已过期，重新登录一次" 
                             if STORE.logged_in() else
                             "。去控制台「音源平台」粘贴 kugou.com 的 Cookie 后再试"),
                          need_login=not STORE.logged_in())
    raise SourceError("酷狗没给播放地址（status=%s）：这首歌可能已下架" % status)


def song_detail(ids):
    if isinstance(ids, (str, int)):
        ids = [ids]
    out = []
    for h in ids:
        h = str(h).strip().upper()
        if not h:
            continue
        try:
            raw = http_get(DETAIL_URL % h, {"Referer": "https://m.kugou.com/",
                                            "User-Agent": MOBILE_UA})
            import json
            d = json.loads(raw.decode("utf-8", "ignore"))
            out.append({"id": h, "name": d.get("songName") or "",
                        "duration": int(d.get("timeLength") or 0)})
        except Exception:
            continue
    return out


_LOGIN_CACHE = {"at": 0.0, "obj": None}


def login_status(force=False):
    """酷狗的 Cookie 只能证明「填过」，不能证明「还有效」：
    会员状态要拿付费曲试才知道，而那样会把状态接口变成一次取流请求。
    这里如实说明，别假装验证过。"""
    base = {"platform": KEY, "label": LABEL, "login": False, "nickname": "",
            "vip": False}
    if not STORE.logged_in():
        base["msg"] = "未配置 Cookie：免费曲照常能翻唱，会员/付费曲取不到"
        return base
    now = time.time()
    if not force and _LOGIN_CACHE["obj"] and now - _LOGIN_CACHE["at"] < 60:
        return _LOGIN_CACHE["obj"]
    obj = dict(base, login=True,
               msg="已保存 Cookie（免费曲不需要它；付费曲取流失败就重新登录一次）")
    _LOGIN_CACHE.update(at=now, obj=obj)
    return obj
