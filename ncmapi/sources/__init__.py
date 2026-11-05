# -*- coding: utf-8 -*-
"""音源适配器注册表。

加新平台只需：写一个模块（照 qq.py / kugou.py 的四个函数），再注册到这里。
3002 网关、面板、后端下载全部按 KEY 走，不用改路由。
"""
from . import kugou, netease, qq
from .base import (COOKIE_DIR, CookieStore, SourceError, make_id, norm_cookie,
                   split_id)

DEFAULT = netease.KEY
REGISTRY = {m.KEY: m for m in (netease, qq, kugou)}

# 别名：老客户端/用户输入可能写 163、qqmusic、kg
ALIAS = {"163": "netease", "netease_cloud": "netease", "wangyi": "netease",
         "qqmusic": "qq", "tencent": "qq", "kg": "kugou", "kugoumusic": "kugou"}


def normalize(platform):
    p = str(platform or "").strip().lower()
    p = ALIAS.get(p, p)
    return p if p in REGISTRY else DEFAULT


def get(platform):
    """取适配器；未知平台退回网易云（老客户端不带 platform 时的行为）。"""
    return REGISTRY[normalize(platform)]


def meta():
    """给面板/网关的静态信息（不发网络请求）。"""
    return [{"key": m.KEY, "label": m.LABEL, "hint": m.COOKIE_HINT,
             "cookie_file": m.STORE.path} for m in REGISTRY.values()]


def search(platform, kw, limit=12, offset=0):
    return get(platform).search(kw, limit, offset)


def song_url(platform, song_id):
    return get(platform).song_url(song_id)


def song_detail(platform, ids):
    return get(platform).song_detail(ids)


def login_status(platform, force=False):
    return get(platform).login_status(force=force)


def store(platform):
    return get(platform).STORE
