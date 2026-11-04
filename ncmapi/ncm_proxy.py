#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地网易云音乐 API（兼容 NeteaseCloudMusicApi 常用端点）
用途：给手机上的 AstrBot 翻唱插件提供「歌名 → 歌曲ID」的搜索能力。
     公网公共实例大面积失效/证书握手失败，本地跑一个最稳，也不受墙和限流影响。

- 端口 3001，绑 0.0.0.0，手机可用 http://电脑IP:3001
- 只用标准库
- 所有请求都记到 access.log，方便看插件到底调了哪些接口

已实现：
  /search  /cloudsearch  /cloudsearch/pc   关键词搜索（返回 ar/al/dt 与 artists/album/duration 双格式）
  /song/detail                            歌曲详情
  /lyric  /song/lyric                     歌词
  /song/url  /song/url/v1                 播放地址（多数需要登录，可能返回 null）
  /search/suggest                         搜索建议
  /comment/music                          热评（R_SO_4_）
  /toplist  /playlist/detail              榜单/歌单
"""
import http.server
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 3002   # 3001 被 D:\snow\index.mjs 占用，这里换到 3002
BASE = "https://music.163.com"
HERE = os.path.dirname(os.path.abspath(__file__))
ACCESS_LOG = os.path.join(HERE, "access.log")
COOKIE_FILE = os.path.join(HERE, "ncm_cookie.txt")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 匿名 Cookie：只能拿非 VIP、非独家曲目
_ANON_COOKIE = "os=pc; appver=2.9.7"


def _load_cookie():
    """从 ncm_cookie.txt 读取登录 Cookie。

    有 MUSIC_U 才算登录态（VIP 生效的前提），否则退回匿名模式。
    返回 (cookie 串, 是否登录)。
    """
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return _ANON_COOKIE, False
    except Exception:
        return _ANON_COOKIE, False

    # 去掉 # 开头的注释行，剩下的拼成 cookie 串；
    # 允许一行写 "MUSIC_U=xxx"，也允许直接粘贴浏览器复制的完整 cookie
    lines = [ln.strip() for ln in raw.splitlines()]
    cookie = " ".join(" ".join(ln for ln in lines if ln and not ln.startswith("#")).split())
    if not cookie or "MUSIC_U=" not in cookie:
        return _ANON_COOKIE, False

    # 补齐 PC 端标识，否则部分接口会拒
    for kv in ("os=pc", "appver=2.9.7"):
        if (kv.split("=")[0] + "=") not in cookie:
            cookie += "; " + kv
    return cookie, True


_COOKIE, VIP_MODE = _load_cookie()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
    "Cookie": _COOKIE,
}


def log(line):
    try:
        with open(ACCESS_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), line))
    except Exception:
        pass


def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def norm_song(s):
    """把 cloudsearch 的 ar/al/dt 与老接口的 artists/album/duration 都补全，兼容两种解析"""
    out = dict(s)
    ar = s.get("ar") or s.get("artists") or []
    al = s.get("al") or s.get("album") or {}
    dt = s.get("dt") or s.get("duration") or 0
    out["ar"] = ar
    out["artists"] = ar
    out["al"] = al
    out["album"] = al
    out["dt"] = dt
    out["duration"] = dt
    out["name"] = s.get("name", "")
    return out


def search_songs(kw, stype="1", limit="10", offset="0"):
    q = urllib.parse.urlencode({"s": kw, "type": stype, "limit": limit, "offset": offset})
    data = fetch("%s/api/cloudsearch/pc?%s" % (BASE, q))
    res = data.get("result") or {}
    songs = [norm_song(s) for s in (res.get("songs") or [])]
    res["songs"] = songs
    res.setdefault("songCount", len(songs))
    data["result"] = res
    data["code"] = 200
    return data


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalNCM/1.0"

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
        ua = (self.headers.get("User-Agent") or "")[:40]
        log("%s %s%s  UA=%s" % (self.client_address[0], path,
                                ("?" + u.query) if u.query else "", ua))
        try:
            if path == "/":
                return self._json({"code": 200, "msg": "local ncm api", "port": PORT})
            if path in ("/search", "/cloudsearch", "/cloudsearch/pc", "/search/get",
                        "/search/get/web"):
                kw = q.get("keywords") or q.get("s") or q.get("key") or q.get("query") or ""
                if not kw:
                    return self._json({"code": 400, "msg": "missing keywords", "result": {"songs": []}})
                return self._json(search_songs(kw, q.get("type", "1"),
                                               q.get("limit", "10"), q.get("offset", "0")))
            if path in ("/song/detail", "/song"):
                ids = q.get("ids") or q.get("id") or ""
                ids = re.sub(r"[^\d,]", "", ids)
                if not ids:
                    return self._json({"code": 400, "songs": []})
                arr = "[" + ",".join(x for x in ids.split(",") if x) + "]"
                data = fetch("%s/api/song/detail?ids=%s" % (BASE, urllib.parse.quote(arr)))
                data["songs"] = [norm_song(s) for s in (data.get("songs") or [])]
                data["code"] = 200
                return self._json(data)
            if path in ("/song/url", "/song/url/v1"):
                ids = re.sub(r"[^\d,]", "", q.get("id") or q.get("ids") or "")
                br = q.get("br") or q.get("level") or "320000"
                url = "%s/api/song/enhance/player/url?ids=[%s]&br=%s" % (
                    BASE, ids, re.sub(r"[^\d]", "", str(br)) or "320000")
                data = fetch(url)
                data["code"] = 200
                return self._json(data)
            if path in ("/lyric", "/song/lyric"):
                sid = re.sub(r"[^\d]", "", q.get("id") or "")
                data = fetch("%s/api/song/lyric?id=%s&lv=1&kv=1&tv=-1" % (BASE, sid))
                data["code"] = 200
                return self._json(data)
            if path == "/search/suggest":
                kw = urllib.parse.quote(q.get("keywords", ""))
                return self._json(fetch("%s/api/search/suggest/web?csrf_token=&s=%s" % (BASE, kw)))
            if path == "/comment/music":
                sid = re.sub(r"[^\d]", "", q.get("id") or "")
                return self._json(fetch(
                    "%s/api/v1/resource/comments/R_SO_4_%s?limit=20&offset=0" % (BASE, sid)))
            if path == "/toplist":
                return self._json(fetch("%s/api/toplist/detail" % BASE))
            if path == "/playlist/detail":
                pid = re.sub(r"[^\d]", "", q.get("id") or "")
                return self._json(fetch("%s/api/v6/playlist/detail?id=%s" % (BASE, pid)))
            # 未实现的接口：返回空结构，避免客户端报解析错
            log("  ↳ 未实现端点，返回空结构: %s" % path)
            return self._json({"code": 200, "result": {"songs": [], "songCount": 0},
                               "songs": [], "data": [], "_note": "endpoint not implemented by local api"})
        except Exception as e:
            log("  ↳ 上游失败: %s: %s" % (type(e).__name__, e))
            return self._json({"code": 500, "msg": "%s: %s" % (type(e).__name__, e),
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
    print("  本地网易云 API 已启动")
    if VIP_MODE:
        print("  登录态:     ✅ 已启用（VIP 曲目可拿完整音源）")
    else:
        print("  登录态:     ⚠️  匿名模式（VIP / 独家曲目会拿不到地址）")
        print("              要启用：双击同目录的 设置网易云Cookie.bat，在窗口里粘贴即可")
    print("  电脑自测:   http://127.0.0.1:%d/search?keywords=十年&limit=3" % PORT)
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
