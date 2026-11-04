# -*- coding: utf-8 -*-
"""安全回归测试：逐条验证本轮加固是否真的生效。

用 8898 起一个临时实例（不碰正在用的 8899），跑完自动恢复 guest_db.json。
    python e2e_security.py
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8898"
os.environ["RVC_NO_AUTOPUMP"] = "1"   # 测试实例不要自动跑真任务

import panel  # noqa: E402
import guest  # noqa: E402
import cover  # noqa: E402
import captcha  # noqa: E402

BASE = "http://127.0.0.1:8898"
DB = guest.DB_PATH
BAK = DB + ".sec_bak"

PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def req(path, method="GET", body=None, headers=None, timeout=20):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        return 0, {}, b""


def main():
    if os.path.isfile(DB):
        shutil.copy2(DB, BAK)

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8898), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    print("\n[1] 安全响应头")
    st, hd, _ = req("/guest")
    chk("访客页 200", st == 200, st)
    chk("CSP 存在", "Content-Security-Policy" in hd)
    chk("X-Frame-Options=DENY", hd.get("X-Frame-Options") == "DENY")
    chk("nosniff", hd.get("X-Content-Type-Options") == "nosniff")

    print("\n[2] 远程（含伪造转发头）进不了管理区")
    # 从回环连进来 + 带 CF 头 = 真反代场景 → 一律当远程，只能看访客页
    st, _, body = req("/admin", headers={"CF-Connecting-IP": "1.2.3.4"})
    chk("/admin 已取消（跳回首页，不再是审批台）", st == 200 and "审批台".encode() not in body, st)
    chk("/admin 不再吐审批台内容", "审批台".encode() not in body)
    st2, _, b2 = req("/api/admin/requests", headers={"CF-Connecting-IP": "1.2.3.4"})
    chk("远程管理接口 403", st2 == 403, st2)
    st3, _, b3 = req("/", headers={"CF-Connecting-IP": "1.2.3.4"})
    chk("远程首页是访客页", st3 == 200 and "控制台".encode() not in b3)
    st4, _, b4 = req("/", headers={"X-Forwarded-For": "5.6.7.8"})
    chk("带 X-Forwarded-For 也当远程", st4 == 200 and "控制台".encode() not in b4)
    st5, _, b5 = req("/")
    chk("本机直连才是控制台", st5 == 200 and "控制台".encode() in b5)

    print("\n[3] CSRF：跨站 Origin 被拒")
    st, _, body = req("/api/guest/request", method="POST",
                      body={"song_id": "1"}, headers={"Origin": "https://evil.example"})
    chk("跨站 POST 返回 403", st == 403, st)
    st, _, _ = req("/api/guest/request", method="POST", body={"song_id": "1"},
                   headers={"Origin": "http://127.0.0.1:8898"})
    chk("同源 POST 不被 CSRF 拦", st != 403, st)

    print("\n[4] 请求体大小上限")
    big = b'{"x":"' + b"a" * (300 * 1024) + b'"}'
    st, _, _ = req("/api/guest/request", method="POST", body=big)
    chk("300KB 请求体被 413 挡下", st == 413, st)

    print("\n[5] 访客 gid 校验")
    g, new = guest.gid_from("rvc_guest=" + "a" * 5000)
    chk("超长 gid 被丢弃并重新生成", new and len(g) == 16, g[:20])
    g2, new2 = guest.gid_from("rvc_guest=<script>alert(1)</script>")
    chk("非法字符 gid 被丢弃", new2, g2[:20])
    g3, _ = guest.gid_from("rvc_guest=0123456789abcdef")
    chk("合法 gid 被接受", g3 == "0123456789abcdef")

    print("\n[6] 模型参数路径穿越")
    res = cover.start_cover({"song_id": "1", "model": "../../../windows/win.ini"})
    chk("穿越路径被拒", not res.get("ok"), res.get("msg"))
    res = guest.create_request("0123456789abcdef", "9.9.9.9", "", "curl/8", {
        "song_id": "1901371647", "title": "x", "model": "../../assets/weights/x.pth"})
    chk("访客提交也拒穿越", not res.get("ok"), res.get("msg"))

    print("\n[7] 按 IP 卡队列（删 Cookie 也绕不过）")
    real_models = [m["name"] for m in cover.models()["models"]]
    if real_models:
        m = real_models[0]
        ip = "9.9.9.1"
        okc = 0
        for i in range(6):
            r = guest.create_request("%016x" % (i + 100), ip, "", "curl/8",
                                     {"song_id": "1901371647", "title": "t%d" % i, "model": m})
            okc += 1 if r.get("ok") else 0
        chk("同 IP 反复换 gid 最多进 3 单", okc <= 3, okc)
        chk("超出后给出提示", okc < 6)
    else:
        print("  ⚠️ 跳过：没有可用模型")

    print("\n[8] 提交频率限制")
    hits = []
    for i in range(12):
        hits.append(guest._sub_allowed("9.9.9.2")[0])
    chk("同 IP 一小时内第 9 单被拦", hits.count(True) == guest.SUB_LIMIT, hits.count(True))

    print("\n[9] 验证码取图频率限制")
    okn = sum(1 for _ in range(captcha.GEN_LIMIT + 5) if captcha.new("g", "8.8.8.8").get("ok"))
    chk("同 IP 取图数被限流", okn == captcha.GEN_LIMIT, okn)

    print("\n[9b] 全局取图总闸（换一堆 IP 也刷不动）")
    captcha._gen_all.clear(); captcha._gen.clear()
    made = 0
    for i in range(captcha.GEN_GLOBAL_LIMIT + 8):
        d = captcha.new("g%d" % i, "10.%d.%d.%d" % (i // 65536 % 256, i // 256 % 256, i % 256))
        made += 1 if d.get("ok") else 0
    chk("全局取图封顶在 %d 张" % captcha.GEN_GLOBAL_LIMIT,
        made == captcha.GEN_GLOBAL_LIMIT, made)
    captcha._gen_all.clear(); captcha._gen.clear()

    print("\n[10] 管理只在本机：没有密码入口")
    st, hd, body = req("/login")
    chk("/login 已取消（跳回首页）", st == 200 and 'type="password"'.encode() not in body, st)
    chk("页面里没有密码表单", b'type="password"' not in body)
    st, _, _ = req("/api/admin/queue", headers={"CF-Connecting-IP": "1.2.3.4"})
    chk("远程看队列 403", st == 403, st)
    st, _, b = req("/api/admin/queue")
    chk("本机看队列正常", st == 200, st)

    print("\n[11] 访客接口不泄露服务器路径")
    st, _, body = req("/api/guest/models")
    d = json.loads(body)
    chk("/api/guest/models 不含绝对路径", "dir" not in d, list(d.keys()))

    print("\n[12] 远程拿不到任何管理数据")
    for p in ("/api/admin/visits", "/api/admin/requests", "/api/cover/covers", "/api/status"):
        st, _, _ = req(p, headers={"CF-Connecting-IP": "1.2.3.4"})
        chk("远程 %s → 403" % p, st == 403, st)

    print("\n[13] 验证码：可重试 + 通行证（一次验证连续搜索）")
    GID = "abcdef0123456789"
    captcha._tokens.clear(); captcha._passes.clear(); captcha._fails.clear()

    d = captcha.new(GID, "5.5.5.1")
    tok = d["token"]
    code = captcha._tokens[tok]["code"]
    wrong = "ZZZZ" if code != "ZZZZ" else "YYYY"
    ok1, _, i1 = captcha.consume(tok, wrong, GID, "5.5.5.1")
    chk("输错 1 次不作废（还剩 2 次）", (not ok1) and i1.get("tries_left") == 2, i1)
    ok2, _, i2 = captcha.consume(tok, "", GID, "5.5.5.1")
    chk("空输入不扣次数", (not ok2) and i2.get("tries_left") == 2, i2)
    captcha.consume(tok, wrong, GID, "5.5.5.1")
    ok3, _, i3 = captcha.consume(tok, wrong, GID, "5.5.5.1")
    chk("连错 3 次才作废", (not ok3) and i3.get("need_new") is True, i3)
    ok4, _, _ = captcha.consume(tok, code, GID, "5.5.5.1")
    chk("作废后连正确码也不认", not ok4)

    d = captcha.new(GID, "5.5.5.2")
    code = captcha._tokens[d["token"]]["code"]
    okp, _, ip_ = captcha.consume(d["token"], code.lower(), GID, "5.5.5.2")
    pt = ip_.get("pass")
    chk("小写输入可通过并发放通行证", okp and bool(pt), bool(pt))
    chk("通行证可直接搜索", captcha.check_pass(pt, GID, "5.5.5.2")[0] is True)

    d = captcha.new(GID, "5.5.5.3")
    code = captcha._tokens[d["token"]]["code"]
    _, _, iq = captcha.consume(d["token"], code, GID, "5.5.5.3")
    pt2 = iq.get("pass")
    chk("通行证换 IP 立刻失效", captcha.check_pass(pt2, GID, "5.5.5.4")[0] is False)

    d = captcha.new(GID, "5.5.5.5")
    code = captcha._tokens[d["token"]]["code"]
    _, _, iq2 = captcha.consume(d["token"], code, GID, "5.5.5.5")
    pt3 = iq2.get("pass")
    used = sum(1 for _ in range(captcha.PASS_QUOTA + 2) if captcha.check_pass(pt3, GID, "5.5.5.5")[0])
    chk("通行证配额 %d 次后失效" % captcha.PASS_QUOTA, used == captcha.PASS_QUOTA, used)

    captcha._fails.clear()
    for _ in range(6):
        captcha._fail("5.5.5.6")
    wait = captcha._wait_secs("5.5.5.6")
    chk("连错 6 次锁 1 分钟", 50 <= wait <= 61, wait)

    # 空关键词不该消耗验证码
    d = captcha.new(GID, "5.5.5.7")
    st, _, body = req("/api/guest/search?kw=&ct=%s&ca=x" % d["token"])
    chk("空关键词不消耗验证码", st == 200 and json.loads(body).get("ok") is False
        and d["token"] in captcha._tokens, json.loads(body).get("msg"))

    print("\n[14] 搜索总闸（同 IP 10 分钟 50 次）")
    hits = [guest._search_allowed("6.6.6.6")[0] for _ in range(guest.SEARCH_LIMIT + 3)]
    chk("第 51 次被拦", hits.count(True) == guest.SEARCH_LIMIT, hits.count(True))

    print("\n[15] keep-alive 请求走私（body 必须被读掉）")
    # 一条带 body 的 GET：body 里藏一条「新请求」。
    # 不读 body 时，残留字节会被当成下一条请求的请求行 —— 藏在 body 里的那条
    # 不带任何转发头，于是被当成本机直连，管理接口就暴露给隧道来客了。
    import socket
    smuggled = b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:8898\r\n\r\n"
    raw = (b"GET /login HTTP/1.1\r\nHost: 127.0.0.1:8898\r\n"
           b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled)
    sk = socket.create_connection(("127.0.0.1", 8898), timeout=6)
    try:
        sk.sendall(raw)
        buf = b""
        while True:
            try:
                chunk = sk.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        sk.close()
    chk("body 里的伪请求没有被执行（只有一条响应）",
        buf.count(b"HTTP/1.1 ") == 1, buf[:120])

    print("\n[16] 就绪探针")
    st, _, b = req("/api/ping")
    chk("/api/ping 公开可读", st == 200 and b"rvc-panel" in b, (st, b[:60]))

    srv.shutdown()
    if os.path.isfile(BAK):
        shutil.move(BAK, DB)

    print("\n" + "=" * 52)
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败：")
        for f in FAIL:
            print("  - " + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
