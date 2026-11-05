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

from .base import CookieBook, SourceError, http_get, http_get_json, try_accounts

KEY = "kugou"
LABEL = "酷狗音乐"
COOKIE_HINT = ("浏览器打开 kugou.com 登录 → F12 → Network → 任意请求 → 复制整段 Cookie"
               "（关键项：token、userid）。免费曲不配也能翻唱，付费曲才需要；可以存多个账号")
COOKIE_NAME = "kugou"
STORE = CookieBook("kugou", ("token=", "userid="))

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


def _headers(cookie=None):
    ck = cookie if cookie is not None else STORE.cookie()
    return {"Referer": "https://www.kugou.com/", "Cookie": ck}


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

    def _one(cookie):
        status = None
        for br in BRS:
            d = http_get_json(TRACKER_URL % (key, h, br), _headers(cookie))
            status = d.get("status")
            urls = d.get("url") or []
            if status == 1 and urls:
                return urls[0]
            if status == 0:
                # key 算错 / 接口改了 —— 换音质也一样，直接报出来，别静默重试
                raise SourceError("酷狗取流被拒（Bad key），可能是接口改了，"
                                  "请在 GitHub 反馈：%s" % (d.get("error") or ""))
            if status == 2:
                # 付费曲：换音质也不会给，直接换下一个账号
                break
        raise SourceError("status=%s（2 = 要会员 / 付费）" % status)

    entries = STORE.entries()
    if entries:
        try:
            url, _acc = try_accounts(entries, _one)
            return url
        except SourceError as e:
            raise SourceError("酷狗取流失败：这首歌要会员 / 付费，或保存的 Cookie 都过期了"
                              "（换一个账号往往就有了）—— %s" % e)
    # 没配账号：免费曲照样能取，取不到就是付费曲
    try:
        return _one("")
    except SourceError as e:
        raise SourceError("酷狗这首歌需要会员 / 付费才能取流（免费曲不配 Cookie 也能翻唱）。"
                          "去控制台「音源平台」粘贴 kugou.com 的 Cookie 后再试 —— %s" % e,
                          need_login=True)


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


_LOGIN_CACHE = {"at": 0.0, "key": "", "obj": [], "one": None}


def accounts_status(force=False):
    """酷狗的 Cookie 只能证明「填过」，不能证明「还有效」：
    会员状态要拿付费曲试才知道，而那样会把状态接口变成一次取流请求。
    这里如实说明，别假装验证过。"""
    items = STORE.accounts()
    out = []
    for x in items:
        row = {"id": x["id"], "file": x["file"], "legacy": x["legacy"],
               "valid": x["valid"], "login": bool(x["valid"]),
               "nickname": "", "vip": None, "msg": ""}
        row["msg"] = ("已保存（免费曲本来就不需要它；付费曲取不到就重新登录一次）"
                      if x["valid"] else "文件里没有 token / userid，不算登录态")
        out.append(row)
    return out


def login_status(force=False):
    accs = accounts_status(force)
    base = {"platform": KEY, "label": LABEL, "login": False, "nickname": "",
            "vip": False, "accounts": len(accs),
            "ready": sum(1 for a in accs if a["login"])}
    if not accs:
        base["msg"] = "未配置 Cookie：免费曲照常能翻唱，会员/付费曲取不到"
        return base
    base.update(login=True,
                msg="" if len(accs) == 1 else "共 %d 个账号（都算「已保存」，验不了活）" % len(accs))
    return base
