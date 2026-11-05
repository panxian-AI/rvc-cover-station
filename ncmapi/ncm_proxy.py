#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地音源 API（网易云 / QQ 音乐 / 酷狗，3002）。

对外仍然是「兼容 NeteaseCloudMusicApi 常用端点」的那套路径，新增一个可选的
`platform` 参数：

    /search?keywords=十年&limit=3&platform=qq
    /song/url?id=0035GveV3i9dBM&platform=qq
    /song/detail?ids=...&platform=kugou
    /platforms                     各家是否配了 Cookie、能否取流
    /login/status?platform=qq

不带 platform 一律当网易云 —— 老插件（AstrBot）、老队列、老成品索引里存的
都是纯数字 id，行为必须原样不变。

各平台的搜索/取流实现都在 sources/ 包里（一个平台一个文件），这里只管
HTTP 外壳、日志和参数解析。
"""
import http.server
import json
import os
import re
import subprocess
import time
import urllib.parse

import sources
from sources.base import SourceError

PORT = 3002   # 3001 被 D:\snow\index.mjs 占用，这里换到 3002
HERE = os.path.dirname(os.path.abspath(__file__))
ACCESS_LOG = os.path.join(HERE, "access.log")

# 老的 3002 只服务网易云，很多地方（README/文档/手机插件）写着这个地址
PUBLIC_PATHS = ("/", "/platforms", "/search", "/cloudsearch", "/cloudsearch/pc",
                "/search/get", "/search/get/web", "/song/detail", "/song",
                "/song/url", "/song/url/v1", "/login/status", "/login/status/detail")


def log(line):
    try:
        with open(ACCESS_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), line))
    except Exception:
        pass


def _song_row(s):
    """统一成「老网易云格式 + 新扁平格式」两种字段，谁都能解析。"""
    return {
        "id": s.get("id", ""),
        "name": s.get("name", ""),
        "artist": s.get("artist", ""),
        "album": s.get("album", ""),
        "duration": int(s.get("duration") or 0),
        "album_id": s.get("album_id", ""),
        # 兼容 NeteaseCloudMusicApi 的 ar/al/dt
        "ar": [{"name": s.get("artist", "")}],
        "al": {"name": s.get("album", ""), "id": s.get("album_id", "")},
        "artists": [{"name": s.get("artist", "")}],
        "albumName": s.get("album", ""),
        "dt": int(s.get("duration") or 0) * 1000,
        "duration_ms": int(s.get("duration") or 0) * 1000,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalMusic/2.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        # 有些客户端用 POST，统一转 GET 处理
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "ignore") if n else ""
        u = urllib.parse.urlparse(self.path)
        merged = u.query + ("&" + body if body else "")
        self.path = u.path + ("?" + merged if merged else "")
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = dict(urllib.parse.parse_qsl(u.query))
        platform = sources.normalize(q.get("platform") or q.get("source") or "netease")
        ua = (self.headers.get("User-Agent") or "")[:40]
        log("%s %s%s  platform=%s UA=%s" % (self.client_address[0], path,
                                            ("?" + u.query) if u.query else "",
                                            platform, ua))
        try:
            if path == "/":
                return self._json({"code": 200, "msg": "local music api", "port": PORT,
                                   "platforms": [m["key"] for m in sources.meta()]})
            if path == "/platforms":
                items = []
                for m in sources.meta():
                    st = sources.login_status(m["key"])
                    items.append(dict(m, login=bool(st.get("login")),
                                      msg=st.get("msg") or "",
                                      nickname=st.get("nickname") or ""))
                return self._json({"code": 200, "platforms": items,
                                   "default": sources.DEFAULT})

            # ---------------- 搜索 ----------------
            if path in ("/search", "/cloudsearch", "/cloudsearch/pc", "/search/get",
                        "/search/get/web"):
                kw = (q.get("keywords") or q.get("s") or q.get("key")
                      or q.get("query") or q.get("w") or "")
                if not kw:
                    return self._json({"code": 400, "msg": "missing keywords",
                                       "result": {"songs": []}})
                limit = min(50, max(1, int(q.get("limit") or 10)))
                offset = max(0, int(q.get("offset") or 0))
                rows = sources.search(platform, kw, limit, offset)
                songs = [_song_row(s) for s in rows]
                return self._json({"code": 200, "platform": platform,
                                   "songs": songs,
                                   "result": {"songs": songs,
                                              "songCount": len(songs)}})

            # ---------------- 详情 ----------------
            if path in ("/song/detail", "/song"):
                ids = q.get("ids") or q.get("id") or ""
                arr = [x for x in re.split(r"[,|]", str(ids)) if x.strip()]
                rows = sources.song_detail(platform, arr)
                return self._json({"code": 200, "platform": platform,
                                   "songs": [_song_row(x) for x in rows]})

            # ---------------- 播放地址 ----------------
            if path in ("/song/url", "/song/url/v1"):
                sid = q.get("id") or q.get("ids") or ""
                sid = str(sid).split(",")[0].strip()
                url = sources.song_url(platform, sid)
                return self._json({"code": 200, "platform": platform,
                                   "data": [{"id": sid, "url": url, "code": 200}]})

            # ---------------- 登录态 ----------------
            if path in ("/login/status", "/login/status/detail"):
                st = sources.login_status(platform, force=("force" in q))
                # 老格式字段（login/nickname/vip）+ 新格式字段（platform/label）
                return self._json({"code": 200, **st})

            # ---------------- 以下只对网易云实现（老插件的其它端点） ----------------
            if path in ("/lyric", "/song/lyric"):
                sid = re.sub(r"[^\d]", "", q.get("id") or "")
                return self._json(dict(sources.get("netease").fetch(
                    "/api/song/lyric?id=%s&lv=1&kv=1&tv=-1" % sid), code=200))
            if path == "/search/suggest":
                kw = urllib.parse.quote(q.get("keywords", ""))
                return self._json(sources.get("netease").fetch(
                    "/api/search/suggest/web?csrf_token=&s=%s" % kw))
            if path == "/comment/music":
                sid = re.sub(r"[^\d]", "", q.get("id") or "")
                return self._json(sources.get("netease").fetch(
                    "/api/v1/resource/comments/R_SO_4_%s?limit=20&offset=0" % sid))
            if path == "/toplist":
                return self._json(sources.get("netease").fetch("/api/toplist/detail"))
            if path == "/playlist/detail":
                pid = re.sub(r"[^\d]", "", q.get("id") or "")
                return self._json(sources.get("netease").fetch(
                    "/api/v6/playlist/detail?id=%s" % pid))

            # 未实现的接口：返回空结构，避免客户端报解析错
            log("  ↳ 未实现端点，返回空结构: %s" % path)
            return self._json({"code": 200, "result": {"songs": [], "songCount": 0},
                               "songs": [], "data": [],
                               "_note": "endpoint not implemented by local api"})
        except SourceError as e:
            log("  ↳ 取流失败: %s" % e)
            return self._json({"code": 404 if not e.need_login else 403,
                               "msg": str(e), "need_login": bool(e.need_login),
                               "platform": platform,
                               "data": [], "result": {"songs": []}}, code=200)
        except Exception as e:
            log("  ↳ 上游失败: %s: %s" % (type(e).__name__, e))
            return self._json({"code": 500, "msg": "%s: %s" % (type(e).__name__, e),
                               "platform": platform,
                               "result": {"songs": []}})


def _local_ips():
    """列出本机所有 IPv4。

    不能用 connect(("8.8.8.8",80)) 探出口 IP —— 双网卡/热点共享时会选中另一块网卡，
    打印出来的地址不是手机能连上的那个（曾误报旧 WiFi 网段）。
    这里只认 ipconfig 里含 IPv4 的行，天然排除网关、子网掩码和 DNS。
    """
    ips = []
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             encoding="gbk", errors="ignore",
                             creationflags=_CREATE_NO_WINDOW).stdout
    except Exception:
        return ips
    for line in out.splitlines():
        if "IPv4" not in line:
            continue
        m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
        if not m:
            continue
        ip = m.group(1)
        if ip == "127.0.0.1" or ip.startswith("169.254."):
            continue
        if ip not in ips:
            ips.append(ip)
    return ips


_CREATE_NO_WINDOW = 0x08000000


def _enable_ansi():
    """打开控制台的 VT 处理（等价于老代码里的 os.system("")）。

    老写法是靠起一个空的 cmd 来「顺手」打开 ANSI，但服务是由面板以 DETACHED
    方式拉起的 —— 没有控制台可继承时，Windows 会给这条 cmd 新分配一个黑窗口，
    用户看到的就是「开启服务时闪一下黑框」。直接改控制台模式，零进程。
    """
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        if not h or h == ctypes.c_void_p(-1).value:
            return
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _wait_enter():
    """有控制台才真的等回车；静默启动时 input() 会抛 EOFError。"""
    try:
        if os.isatty(0):
            input("按回车退出…")
    except Exception:
        pass


def _login_line(platform):
    st = sources.login_status(platform)
    name = sources.get(platform).LABEL
    if st.get("login"):
        extra = ("：" + st["nickname"]) if st.get("nickname") else ""
        return "  %-10s ✅ 已启用%s" % (name, extra)
    return "  %-10s ⚠️  %s" % (name, st.get("msg") or "未登录")


def main():
    _enable_ansi()
    try:
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print("端口 %d 启动失败: %s" % (PORT, e))
        _wait_enter()
        return
    ips = _local_ips()
    print("=" * 56)
    print("  本地音源 API 已启动（网易云 / QQ 音乐 / 酷狗）")
    for p in ("netease", "qq", "kugou"):
        print(_login_line(p))
    print("  配置 Cookie：控制台 http://127.0.0.1:8899 → 概览 → 音源平台")
    print("  电脑自测:   http://127.0.0.1:%d/search?keywords=十年&limit=3&platform=qq" % PORT)
    if ips:
        print("  手机插件填: http://%s:%d" % (ips[0], PORT))
        for extra in ips[1:]:
            print("          或: http://%s:%d" % (extra, PORT))
        if len(ips) > 1:
            print("              （有多个网卡，逐个试；监控面板也会列出全部地址）")
    else:
        print("  手机插件填: http://本机IP:%d （可用监控面板查看）" % PORT)
    print("  请求日志:   %s" % ACCESS_LOG)
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
