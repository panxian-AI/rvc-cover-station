# -*- coding: utf-8 -*-
"""页面与静态资源登记表（多页面）。

控制台从「一个 1300 行的单页」拆成若干独立页面，共用 assets/ 下的样式与脚本。
每页都在这里登记，路由只要查表，不许在 handler 里硬写文件名 —— 加一页只改这一处。
"""
import os

from . import core

# key -> (文件名, 缺失时的兜底 HTML)
PAGES = {
    "console": ("console_page.html",
                "<h1>控制台页面缺失</h1><p>dashboard/console_page.html 不见了</p>"),
    "tasks": ("console_tasks_page.html",
              "<h1>任务页缺失</h1><p>dashboard/console_tasks_page.html 不见了</p>"),
    "guest": ("console_guest_page.html",
              "<h1>访客页缺失</h1><p>dashboard/console_guest_page.html 不见了</p>"),
    "settings": ("console_settings_page.html",
                 "<h1>设置页缺失</h1><p>dashboard/console_settings_page.html 不见了</p>"),
    "legacy": ("legacy_page.html",
               "<h1>旧版监控页缺失</h1><p>dashboard/legacy_page.html 不见了</p>"),
    "market": ("market_page.html",
               "<h1>模型市场页面缺失</h1><p>dashboard/market_page.html 不见了</p>"),
}

# 路径 -> 页面 key。只认本机直连（路由那边已经拦过一遍）
ROUTES = {
    "/": "console",
    "/index.html": "console",
    "/console": "console",
    "/console/tasks": "tasks",
    "/console/guest": "guest",
    "/console/settings": "settings",
    "/panel": "legacy",
    "/market": "market",
}

ASSET_DIR = os.path.join(core.HERE, "assets")

_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
}


def page(key):
    """取页面内容（按 mtime 缓存，改完刷新浏览器就生效）。"""
    item = PAGES.get(key)
    if not item:
        return ""
    return core._read_page(item[0], item[1])


def key_for(path):
    return ROUTES.get(path)


def asset_abs(name):
    """把 /assets/xxx 换成真实路径；越界/未知后缀一律 None。

    只允许 assets 目录下一层，不做递归目录，避免 ../ 摸到别处去。
    """
    name = str(name or "").replace("\\", "/")
    # ":" 必须挡：os.path.join("E:\\...\\assets", "C:/x.js") 会**丢掉**前半段，
    # 变成真正的 C:\x.js（盘符只算一段，len(parts)>2 拦不住）。
    if not name or name.startswith("/") or ".." in name or "\x00" in name or ":" in name:
        return None
    if "/" in name.strip("/"):
        # 允许一级子目录（如 assets/fonts/x.woff2），但不许再深
        parts = [p for p in name.split("/") if p]
        if len(parts) > 2:
            return None
    p = os.path.join(ASSET_DIR, name.replace("/", os.sep))
    # 最后再兜一次：真实路径必须仍在 assets 目录下（符号链接/短名也拦）
    real = os.path.realpath(p)
    try:
        if os.path.commonpath([real, os.path.realpath(ASSET_DIR)]) != os.path.realpath(ASSET_DIR):
            return None
    except ValueError:            # 不同盘符
        return None
    if not os.path.isfile(real):
        return None
    if os.path.splitext(real)[1].lower() not in _TYPES:
        return None
    return real


def asset_type(path):
    return _TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def asset_version():
    """所有静态资源的「最后改动时间」最大值，页面拿它当 cache buster。

    改完 css/js 刷新页面就能拿到新的，不用 Ctrl+F5（浏览器缓存按 URL 走）。
    """
    latest = 0.0
    try:
        for n in os.listdir(ASSET_DIR):
            p = os.path.join(ASSET_DIR, n)
            if os.path.isfile(p):
                latest = max(latest, os.path.getmtime(p))
    except OSError:
        return "0"
    return str(int(latest))
