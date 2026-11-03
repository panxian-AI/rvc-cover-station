# -*- coding: utf-8 -*-
"""验证码锁定 + 「请求站长解锁（只解一次）」回归测试。

用 8898 起一个临时实例（不碰正在用的 8899），用 CF-Connecting-IP 头模拟
公网访客 IP，验证：锁定阶梯、访客请求限频、管理员审批、同意只解一次
（再错照常再锁）、拒绝流程、公网/远程权限边界。
    python e2e_unlock.py
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
os.environ["RVC_NO_AUTOPUMP"] = "1"

import panel  # noqa: E402
import guest  # noqa: E402
import captcha  # noqa: E402

BASE = "http://127.0.0.1:8898"
DB = guest.DB_PATH
BAK = DB + ".unlock_bak"

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
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        return 0, b""


def jreq(path, method="GET", body=None, headers=None):
    st, raw = req(path, method, body, headers)
    try:
        return st, json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return st, {}


def visitor_ip(ip):
    return {"CF-Connecting-IP": ip}


def main():
    if os.path.isfile(DB):
        shutil.copy2(DB, BAK)

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8898), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    VIP = "9.9.9.9"      # 主角访客：走「请求 → 同意 → 再错再锁」全流程
    DIP = "8.8.8.8"      # 配角访客：走「拒绝」流程
    VH = visitor_ip(VIP)

    print("\n[1] 未锁定时的边界")
    st, d = jreq("/api/guest/lock", headers=VH)
    chk("访客可查自己的锁定状态", st == 200 and d.get("locked") is False, d)
    st, d = jreq("/api/guest/unlock_request", method="POST", body={}, headers=VH)
    chk("没被锁时请求解锁被拒（提示不用申请）",
        st == 200 and d.get("ok") is False and "没有" in d.get("msg", ""), d)

    print("\n[2] 锁定阶梯（连错 6 次锁 1 分钟）")
    for _ in range(6):
        captcha._fail(VIP)
    info = captcha.lock_info(VIP)
    chk("连错 6 次进入锁定", info["locked"] and 0 < info["left"] <= 61, info)
    chk("连错计数为 6", info["streak"] == 6, info)

    print("\n[3] 锁定时的访客提示与请求")
    tok = captcha.new(gid="g1", ip=VIP)
    ok, msg, info = captcha.consume(tok["token"], "XXXX", gid="g1", ip=VIP)
    chk("锁定时搜索被拦且消息明确", not ok and "临时锁定" in msg, msg)
    chk("锁定时报错带 locked/left 字段", info.get("locked") is True and info.get("left", 0) > 0, info)
    st, d = jreq("/api/guest/unlock_request", method="POST", body={}, headers=VH)
    chk("锁定中可发解锁请求", st == 200 and d.get("ok") is True, d)
    st, d = jreq("/api/guest/unlock_request", method="POST", body={}, headers=VH)
    chk("10 分钟内重复请求被限频", d.get("ok") is False and "不能再发" in d.get("msg", ""), d)
    st, d = jreq("/api/guest/lock", headers=VH)
    chk("轮询能看到 pending 状态", d.get("status") == "pending" and d.get("locked") is True, d)

    print("\n[4] 权限边界：管理接口远程不可用")
    st, raw = req("/api/admin/unlock", headers=VH)
    chk("公网访客拉审批列表 403", st == 403, st)
    st, raw = req("/api/admin/unlock", method="POST", body={"ip": VIP, "allow": True},
                  headers=VH)
    chk("公网访客审批 403（且未生效）", st == 403 and captcha.lock_info(VIP)["locked"], st)

    print("\n[5] 站长审批（同意 = 只解一次）")
    st, d = jreq("/api/admin/unlock")
    chk("本机可拉待处理列表", st == 200 and d.get("ok") and d.get("count") == 1, d)
    row = (d.get("pending") or [{}])[0]
    chk("列表带 IP / 连错次数 / 等待时长",
        row.get("ip") == VIP and row.get("streak") == 6 and "age" in row, row)
    st, d = jreq("/api/admin/unlock", method="POST", body={"ip": "7.7.7.7", "allow": True})
    chk("审批不存在的请求报错", d.get("ok") is False, d)
    st, d = jreq("/api/admin/unlock", method="POST", body={"ip": VIP, "allow": True})
    chk("同意成功", d.get("ok") is True and "已同意" in d.get("msg", ""), d)
    info = captcha.lock_info(VIP)
    chk("同意后锁定立即解除", info["locked"] is False, info)
    chk("同意保留连错计数（不是清零豁免）", info["streak"] == 6, info)

    print("\n[6] 一次性：再错照样再锁")
    for _ in range(3):
        captcha._fail(VIP)          # streak 6→9，第二次阶梯
    info = captcha.lock_info(VIP)
    chk("再错 3 次立即按阶梯再锁（约 3 分钟）",
        info["locked"] and 120 < info["left"] <= 181 and info["streak"] == 9, info)
    st, d = jreq("/api/guest/unlock_request", method="POST", body={}, headers=VH)
    chk("刚被同意过，10 分钟内不能立刻再请求", d.get("ok") is False, d)

    print("\n[7] 拒绝流程")
    for _ in range(6):
        captcha._fail(DIP)
    st, d = jreq("/api/guest/unlock_request", method="POST", body={}, headers=visitor_ip(DIP))
    chk("配角访客可发请求", d.get("ok") is True, d)
    st, d = jreq("/api/admin/unlock", method="POST", body={"ip": DIP, "allow": False})
    chk("拒绝成功", d.get("ok") is True and "拒绝" in d.get("msg", ""), d)
    st, d = jreq("/api/guest/lock", headers=visitor_ip(DIP))
    chk("访客轮询看到被拒状态", d.get("status") == "no" and d.get("locked") is True, d)
    info = captcha.lock_info(DIP)
    chk("拒绝后锁定原样保留", info["locked"] is True, info)
    st, d = jreq("/api/admin/unlock", method="POST", body={"ip": DIP, "allow": True})
    chk("已处理过的请求不能重复批", d.get("ok") is False and "处理过" in d.get("msg", ""), d)

    print("\n[8] 请求过期清理")
    captcha._ulreqs[VIP]["t"] = time.time() - captcha.ULREQ_TTL - 10
    st, d = jreq("/api/guest/unlock_request", method="POST", body={}, headers=VH)
    chk("过期请求不挡新请求（可再次申请）", d.get("ok") is True, d)

    print("\n" + "=" * 52)
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败：")
        for f in FAIL:
            print("  - " + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
