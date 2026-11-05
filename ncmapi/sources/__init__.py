# -*- coding: utf-8 -*-
"""音源适配器注册表。

加新平台只需：写一个模块（照 qq.py / kugou.py 的四个函数），再注册到这里。
3002 网关、面板、后端下载全部按 KEY 走，不用改路由。
"""
import re
import threading

from . import kugou, netease, qq
from .base import (COOKIE_DIR, CookieBook, CookieStore, SourceError, make_id,
                   norm_cookie, split_id)

DEFAULT = netease.KEY
REGISTRY = {m.KEY: m for m in (netease, qq, kugou)}
ORDER = [netease.KEY, qq.KEY, kugou.KEY]

# 别名：老客户端/用户输入可能写 163、qqmusic、kg
ALIAS = {"163": "netease", "netease_cloud": "netease", "wangyi": "netease",
         "qqmusic": "qq", "tencent": "qq", "kg": "kugou", "kugoumusic": "kugou"}
# 聚合搜索的写法
ALL_ALIAS = {"all", "any", "*", "all_platforms", "multi"}

_PREFIXED = re.compile(r"^(netease|qq|kugou)[:\-][A-Za-z0-9]{1,40}$", re.I)


def normalize(platform):
    p = str(platform or "").strip().lower()
    p = ALIAS.get(p, p)
    return p if p in REGISTRY else DEFAULT


def is_all(platform):
    return str(platform or "").strip().lower() in ALL_ALIAS


def get(platform):
    """取适配器；未知平台退回网易云（老客户端不带 platform 时的行为）。"""
    return REGISTRY[normalize(platform)]


def meta():
    """给面板/网关的静态信息（不发网络请求）。"""
    out = []
    for m in REGISTRY.values():
        store = m.STORE
        legacy = getattr(store, "legacy_paths", None)
        out.append({
            "key": m.KEY, "label": m.LABEL, "hint": m.COOKIE_HINT,
            "cookie_dir": getattr(store, "dir", ""),
            "cookie_file": (legacy[0] if legacy else getattr(store, "path", "")),
            "accounts": len(store.entries()),
        })
    return out


def _target(platform, song_id):
    """带前缀的 id 自己说了算，否则听 platform 参数。"""
    raw = str(song_id or "")
    if _PREFIXED.match(raw):
        return split_id(raw)
    return normalize(platform), raw


def search(platform, kw, limit=12, offset=0):
    if is_all(platform):
        rows, _errs = search_all(kw, limit)
        return rows
    return get(platform).search(kw, limit, offset)


def search_all(kw, limit=12, timeout=15):
    """并发打所有平台，**交错**合并。

    为什么交错而不是「默认平台排前面」：三家搜同一首歌时，用户第一眼要看的是
    「哪家能拿到」，不是「网易云的全部结果 + 另外两家沉底」。
    单家失败（网络 / 被风控）不影响其它家的结果，原因一并回给调用方。
    """
    buckets = {}
    errs = []
    lock = threading.Lock()

    def work(m):
        try:
            rows = m.search(kw, limit) or []
        except Exception as e:
            with lock:
                errs.append("%s：%s: %s" % (m.LABEL, type(e).__name__, e))
            return
        with lock:
            buckets[m.KEY] = rows

    threads = [threading.Thread(target=work, args=(REGISTRY[k],), daemon=True)
               for k in ORDER]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    out = []
    deep = max([len(buckets.get(k) or []) for k in ORDER] or [0])
    for i in range(deep):
        for k in ORDER:
            rows = buckets.get(k) or []
            if i < len(rows):
                row = dict(rows[i])
                row["platform"] = k
                row["platform_label"] = REGISTRY[k].LABEL
                out.append(row)
    return out, errs


def song_url(platform, song_id):
    plat, sid = _target(platform, song_id)
    return get(plat).song_url(sid)


def song_detail(platform, ids):
    if isinstance(ids, (str, int)):
        ids = [ids]
    plain = [x for x in ids if not _PREFIXED.match(str(x))]
    if len(plain) != len(ids):
        # 混着带前缀的 id：按前缀分组，各找各的适配器
        out = []
        groups = {}
        for x in ids:
            plat, sid = _target(platform, x)
            groups.setdefault(plat, []).append(sid)
        for plat, sids in groups.items():
            try:
                out.extend(get(plat).song_detail(sids))
            except Exception:
                continue
        return out
    return get(platform).song_detail(plain)


def login_status(platform, force=False):
    return get(platform).login_status(force=force)


def accounts(platform, force=False):
    """某平台所有账号的登录态明细。"""
    return get(platform).accounts_status(force=force)


def accounts_all(force=False):
    return {k: REGISTRY[k].accounts_status(force=force) for k in ORDER}


def store(platform):
    return get(platform).STORE


def logout(platform, account):
    """退出（删掉）某个账号的 Cookie。"""
    return get(platform).STORE.remove(account)


def logout_all(platform):
    return get(platform).STORE.clear()
