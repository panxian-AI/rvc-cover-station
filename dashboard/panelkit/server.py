# -*- coding: utf-8 -*-
"""HTTP 服务：连接上限、Handler 基类（响应/鉴权/读体）、路由分发。"""
import http.server
import json
import os
import re
import socket
import threading
import urllib.parse

from .core import *          # noqa: F401,F403
from . import core, pages, security
from .security import _ban_left, _probe_hit, public_paused, sec_log
from .sysinfo import lan_ips
from . import routes_get, routes_post


# ---------------- HTTP 服务 ----------------
# 同时处理的连接上限。ThreadingHTTPServer 是「一连接一线程、不设上限」，
# 公网被人开几百个慢连接（连上来不发完整请求，卡满 timeout）就能把线程和内存耗光，
# 正经访客反而打不开。这里用信号量卡住上限，超出的直接回 503。
MAX_CONN = 96


class PanelServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # 必须独占端口。Windows 的 SO_REUSEADDR 语义不是「快速重启」，而是「允许抢占
    # 已被别的进程占用的端口」——设成 True 时，重复双击启动脚本会静默叠出好几个
    # 实例，全都 accept 同一个 8899，请求随机落到其中某一个。旧实例跑的是改代码
    # 之前的路由，于是同一个地址一会儿 200 一会儿 404（新增页面「看不见」就是这么
    # 来的）。设成 False 后第二个实例会 bind 失败，走 main() 的「已在运行」分支。
    allow_reuse_address = False
    request_queue_size = 128

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(MAX_CONN)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Retry-After: 5\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()          # 线程没起来，槽位要还回去
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 20             # 慢连接/半开连接不许一直占线程（Slowloris）

    def log_message(self, *a):
        pass

    # 统一安全响应头：防点击劫持、防 MIME 嗅探、限制外链/内联注入面
    SEC_HEADERS = [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
        ("Content-Security-Policy",
         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
         "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
         "base-uri 'none'; form-action 'self'"),
    ]

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self.SEC_HEADERS:
            self.send_header(k, v)
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where):
        """302 跳到别处（不带查询串，避免把参数粘出去）。"""
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in self.SEC_HEADERS:
            self.send_header(k, v)
        self.end_headers()

    def _send_file(self, path):
        """直接把 mp3 吐出去（成品试听 / 下载）"""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            # 不回显绝对路径/系统错误，避免泄露服务器目录结构
            self._send(404, json.dumps({"ok": False, "msg": "文件不存在"}, ensure_ascii=False))
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_download(self, path, name=None):
        """发一个附件（数据备份 zip）。名字可能是中文，按 RFC 5987 双写。"""
        name = os.path.basename(str(name or path or "download.zip"))
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            self._send(404, json.dumps({"ok": False, "msg": "文件不存在"}, ensure_ascii=False))
            return
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "backup.zip"
        quoted = urllib.parse.quote(name)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                         % (ascii_name, quoted))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError:
            pass
        finally:
            f.close()

    # ---------- 访问控制（公网穿透后必须） ----------
    def _client(self):
        """返回 (真实IP, 是否经反代)。

        cloudflared 连本地时来源恒为 127.0.0.1，真实 IP 只在 CF-Connecting-IP /
        X-Forwarded-For 里。**只有连接本身来自回环时才信任这些头**：否则局域网里
        的机器可以随便伪造头冒充任意 IP（绕过登录失败锁定）。
        """
        peer = (self.client_address[0] or "").replace("::ffff:", "")
        if peer in ("127.0.0.1", "::1"):
            # 除了真实 IP，也认 Cloudflare 自己加的几个头：万一哪天 CF-Connecting-IP
            # 被前置代理剥掉，只要还留着 CF-Ray / CF-Visitor 就不会被当成「本机直连」。
            for h in ("CF-Connecting-IP", "X-Forwarded-For", "CF-Ray",
                      "CF-Visitor", "X-Real-IP"):
                v = self.headers.get(h)
                if v:
                    return v.split(",")[0].strip() or "0.0.0.0", True
        return peer or "0.0.0.0", False

    def _host_ok(self):
        """Host 头必须是本机名/回环/本机网卡 IP。

        为什么查这个：防 DNS rebinding —— 恶意站点把域名解析到 127.0.0.1，
        浏览器发来的请求 peer 是回环、又没有转发头，不查 Host 的话会被当成站长，
        网页里的 JS 就能读管理接口、点「一键更新」。隧道域名不在白名单里，
        但它本来就带转发头、走不到本机判定。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            base = host.split("]")[0] + "]"
            rest = host[len(base):]
            port = rest[1:] if rest.startswith(":") else ""
        else:
            base, _, port = host.partition(":")
        if port and not port.isdigit():
            return False
        if base in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return True
        try:
            for x in lan_ips():
                if base and base == str(x.get("ip") or "").lower():
                    return True
        except Exception:
            pass
        return False

    def _is_local(self):
        """是否本机直连：回环地址、**没有**任何转发头、且 Host 是本机地址。

        cloudflared 连本地时来源也是 127.0.0.1，所以必须同时看有没有转发头，
        否则公网来客会被误判成站长。Host 校验再加一道，防 DNS rebinding。
        """
        ip, proxied = self._client()
        return (not proxied) and ip in ("127.0.0.1", "::1") and self._host_ok()

    def _local_only(self, as_json=True):
        """True=放行；False=已回 403。

        管理功能只认本机直连。公网（隧道）、局域网、以及带了转发头的本机脚本
        一律拒绝 —— 没有密码这层，也就没有「远程登进来管理」这回事。
        """
        if self._is_local():
            return True
        ip, _ = self._client()
        sec_log("拒绝远程管理 ip=%s path=%s ua=%s" % (
            ip, (self.path or "")[:120], (self.headers.get("User-Agent") or "")[:100]), ip=ip)
        if as_json:
            self._send(403, json.dumps({"ok": False, "local_only": True,
                                        "msg": MSG_LOCAL_ONLY}, ensure_ascii=False))
        else:
            self._send(403, MSG_LOCAL_ONLY, "text/plain; charset=utf-8")
        return False

    def _maintenance(self, as_json=False):
        """公网访问被站长暂停时的统一响应（本机控制台不受影响）。"""
        if as_json:
            self._send(503, json.dumps({
                "ok": False, "paused": True,
                "msg": "站长暂时关闭了公网访问，请稍后再来"}, ensure_ascii=False))
            return
        html = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>维护中</title><style>body{background:#0f1420;color:#e8eefc;"
                "font-family:-apple-system,'Microsoft YaHei',sans-serif;display:flex;"
                "align-items:center;justify-content:center;height:100vh;margin:0;padding:20px}"
                ".b{background:#182031;border:1px solid #2a3550;border-radius:16px;"
                "padding:26px 28px;max-width:420px;text-align:center;line-height:1.8}"
                "h1{font-size:17px;margin:0 0 8px}p{margin:0;color:#9fb0cc;font-size:13px}"
                "</style></head><body><div class=\"b\"><h1>🛠 暂时关闭中</h1>"
                "<p>站长暂停了公网访问，稍后再来试试。</p></div></body></html>")
        self._send(503, html, "text/html; charset=utf-8")

    def _csrf_ok(self):
        """跨站请求防护：带了 Origin 就必须和本站同源。

        浏览器发起的跨站 POST 一定带 Origin；curl / 本机脚本不带，因此不受影响。
        配合 Cookie 的 SameSite=Lax，构成双保险。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            o = urllib.parse.urlparse(origin)
        except Exception:
            return False
        return bool(o.netloc) and o.netloc.strip().lower() == host


    MAX_BODY = 256 * 1024        # 单个请求体上限，防止拿 Content-Length 撑爆内存

    def _read_json(self):
        self._body_too_big = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < 0:
            n = 0
        if n > self.MAX_BODY:
            # 只读掉上限那么多，剩下的不读并断开连接（否则连接复用会错位）
            try:
                self.rfile.read(self.MAX_BODY)
            except Exception:
                pass
            self._body_too_big = True
            self.close_connection = True
            return {}
        try:
            raw = self.rfile.read(n) if n else b""
            obj = json.loads(raw.decode("utf-8", "ignore") or "{}")
            # 只接受 JSON 对象：数组/字符串/数字会让后面 body.get 直接 AttributeError
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _too_big(self):
        """请求体超限时回 413，返回 True 表示已响应。"""
        if not getattr(self, "_body_too_big", False):
            return False
        self._send(413, json.dumps({"ok": False, "msg": "请求体过大"}, ensure_ascii=False))
        return True

    def _send_asset(self, name):
        """发 assets/ 下的静态资源（控制台共用的 css/js）。

        只认 pages.asset_abs 给出的路径（一层目录、白名单后缀），越界直接 404；
        缓存时间给 10 分钟但带版本号查询串，改完刷新就是新的。
        """
        p = pages.asset_abs(name)
        if not p:
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, json.dumps({"error": "not found"}))
            return
        self.send_response(200)
        self.send_header("Content-Type", pages.asset_type(p))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 路由：具体实现在 routes_get.py / routes_post.py ----------
    def do_GET(self):
        routes_get.handle(self)

    def do_POST(self):
        routes_post.handle(self)
