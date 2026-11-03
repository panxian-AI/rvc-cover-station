# -*- coding: utf-8 -*-
"""安全加固回归测试（白名单防冒用 + 公网暴露面收敛）。

覆盖两件事：
  A. 白名单身份不能被「同款设备 / 抄来的 gid / 多台设备」顶着用
     —— 设备密钥绑定、每日/每小时额度、最小间隔、多设备自动挂起
  B. 隧道被扫也进不了其它内容
     —— 公网只认白名单路径（fail-closed）、Host 校验防 DNS rebinding、
        探测计数与临时封禁、一键暂停公网访问

用 8896 起临时实例，数据库/设置/公网开关全部指到临时目录，跑完自动清理。
    python e2e_hardening.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="rvc_hard_")
os.environ["RVC_PANEL_PORT"] = "8896"
os.environ["RVC_NO_AUTOPUMP"] = "1"
os.environ["RVC_GUEST_DB"] = os.path.join(TMP, "guest_db.json")
os.environ["RVC_SETTINGS_PATH"] = os.path.join(TMP, "advanced_settings.json")
os.environ["RVC_PUBLIC_STATE"] = os.path.join(TMP, "public_access.json")

import panel   # noqa: E402
import guest   # noqa: E402
import cover   # noqa: E402

BASE = "http://127.0.0.1:8896"
PASS, FAIL = [], []

FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
DV_A = "1" * 48
DV_B = "2" * 48
DV_C = "3" * 48
IP_HOME = "203.0.113.10"


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def req(path, method="GET", body=None, headers=None, timeout=15):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        return 0, {}, b""


def js(body):
    try:
        return json.loads(body.decode("utf-8", "ignore") or "{}")
    except Exception:
        return {}


PUB = {"CF-Connecting-IP": "198.51.100.9"}          # 模拟经隧道进来的公网访客


def pub_get(path):
    return req(path, headers=PUB)


def pub_post(path, body):
    h = dict(PUB)
    return req(path, "POST", body, headers=h)


def wl_reset():
    with guest._lock:
        guest._db["wl_apps"] = []
        guest._db["whitelist"] = []
        guest._save()


def approve(gid):
    with guest._lock:
        aid = next((a["id"] for a in guest._db["wl_apps"]
                    if a.get("gid") == gid and a.get("state") == "pending"), None)
    s, _h, b = req("/api/admin/wl_decide", "POST",
                   {"id": aid, "action": "approve", "note": "测试"})
    return s == 200 and js(b).get("ok")


def entry(gid):
    with guest._lock:
        return guest._wl_entry_by(gid)


def main():
    calls = []

    def stub(jid, song_id, title, artist, model, key_shift, acapella, key=None):
        calls.append(song_id)
        cover._update(jid, state="running", started=time.time(), stage="桩任务")
        time.sleep(0.4)
        cover._update(jid, state="done", stage="完成", ended=time.time(), file=None, size_text="桩")

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8896), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)
    try:
        cover._run_cover = stub
        model = (cover.models().get("models") or [{}])[0].get("name")
        if not model:
            chk("硬盘上有可用声线", False)
            return

        # ============ A. 白名单身份 ============
        print("\n== 1. 设备密钥绑定（同款设备抄指纹也进不来） ==")
        wl_reset()
        g1 = "1111111111111111"
        r = guest.wl_apply(g1, FP_A, IP_HOME, "", "ua", "我是老用户想免审批，谢谢", DV_A)
        chk("带设备密钥的申请可提交", r.get("ok"), r.get("msg"))
        chk("批准成功", approve(g1))
        e = entry(g1) or {}
        chk("名单里标为已绑设备密钥", bool(e.get("dv_hash")), e.get("dv_hash", "")[:8])
        chk("落库不存明文密钥", DV_A not in json.dumps(e))
        chk("带对密钥 → 命中", guest.wl_check(g1, FP_A, IP_HOME, DV_A)[0] == "ok")
        chk("不带密钥 → 不命中", guest.wl_check(g1, FP_A, IP_HOME, "")[0] == "mismatch")
        chk("抄了 gid+指纹但密钥不对 → 不命中",
            guest.wl_check(g1, FP_A, IP_HOME, DV_B)[0] == "mismatch")
        r = guest.create_request(g1, IP_HOME, "", "ua",
                                {"type": "cover", "song_id": "900000201", "title": "冒用测试",
                                 "artist": "x", "model": model, "fp": FP_A, "dv": DV_B})
        rid = (r.get("request") or {}).get("id")
        chk("拿错密钥提交 → 降级为人工审批（不直通）",
            "白名单直通" not in (r.get("msg") or "") and
            (rid is None or guest._find(rid).get("state") == "pending"),
            r.get("msg"))
        with guest._lock:
            for x in guest._db["requests"]:
                if x.get("id") == rid:
                    x["state"] = "rejected"
            guest._save()

        print("\n== 2. 老条目（只绑指纹）仍可用，但会被标记 ==")
        with guest._lock:
            guest._db["whitelist"].append({
                "gid": "9999999999999999", "fp": FP_C, "ip": IP_HOME,
                "ua": "老设备", "note": "老版本批的", "approved": time.time(),
                "hits": 0, "last_seen": 0})
            guest._save()
        chk("老条目无密钥也能命中（向后兼容）",
            guest.wl_check("9999999999999999", FP_C, IP_HOME, "")[0] == "ok")
        s, _h, b = req("/api/admin/wl")
        d = js(b)
        old = [x for x in (d.get("entries") or []) if x["gid"] == "9999999999999999"]
        chk("控制台把它标成「仅指纹」", bool(old) and old[0].get("device_bound") is False)
        chk("计数里给出待升级数量", d.get("counts", {}).get("legacy", 0) >= 1)

        print("\n== 3. 额度：抄走身份也刷不穿 ==")
        w = entry(g1)
        with guest._lock:
            w["uses"] = [time.time() - 5]        # 刚做过一单
            guest._save()
        st, msg, _ = guest.wl_check(g1, FP_A, IP_HOME, DV_A)
        chk("20 秒内第二单被挡（gap）", st == "gap", (st, msg))
        r = guest.create_request(g1, IP_HOME, "", "ua",
                                {"type": "cover", "song_id": "900000202", "title": "太快了",
                                 "artist": "x", "model": model, "fp": FP_A, "dv": DV_A})
        chk("提交接口直接回提示（不排队）", not r.get("ok") and "秒" in (r.get("msg") or ""),
            r.get("msg"))
        with guest._lock:
            w["uses"] = [time.time() - 60] * guest.WL_DAY_QUOTA
            guest._save()
        st, msg, _ = guest.wl_check(g1, FP_A, IP_HOME, DV_A)
        chk("日额度用完被挡（quota）", st == "quota", (st, msg))
        with guest._lock:
            w["uses"] = [time.time() - 100] * (guest.WL_HOUR_QUOTA + 1)
            guest._save()
        chk("小时额度用完也被挡", guest.wl_check(g1, FP_A, IP_HOME, DV_A)[0] == "quota")

        print("\n== 4. 多设备冒用 → 自动挂起 + 告警 + 可恢复 ==")
        wl_reset()
        g2 = "2222222222222222"
        guest.wl_apply(g2, FP_A, IP_HOME, "", "ua", "我要申请白名单，常用用户", DV_A)
        chk("批准 g2", approve(g2))
        base = int(time.time())
        with guest._lock:
            w2 = guest._wl_entry_by(g2)
            w2["miss"] = [{"fp": x, "ip": IP_HOME, "k": "fp", "t": base - 60}
                          for x in (FP_B, FP_C)]
            guest._save()
        chk("第 3 个不同指纹出现 → 判定冒用",
            guest.wl_check(g2, "d" * 64, IP_HOME, DV_B)[0] != "ok")
        chk("该身份已被挂起", bool((entry(g2) or {}).get("suspended")),
            (entry(g2) or {}).get("suspend_reason"))
        chk("挂起后即使要素全对也不放行",
            guest.wl_check(g2, FP_A, IP_HOME, DV_A)[0] == "suspended")
        r = guest.create_request(g2, IP_HOME, "", "ua",
                                {"type": "cover", "song_id": "900000203", "title": "挂起测试",
                                 "artist": "x", "model": model, "fp": FP_A, "dv": DV_A})
        chk("挂起身份提交被拒", not r.get("ok") and "暂停" in (r.get("msg") or ""), r.get("msg"))
        s, _h, b = req("/api/admin/wl")
        d = js(b)
        chk("控制台有告警条目", any(a["gid"] == g2 for a in d.get("alerts") or []),
            d.get("alerts"))
        chk("告警里有原因", any("指纹" in (a.get("reason") or "") for a in d.get("alerts") or []))
        chk("访客自己能看到被暂停",
            guest.wl_mine(g2, FP_A, IP_HOME, DV_A).get("state") == "suspended")
        s, _h, b = req("/api/admin/wl_state", "POST", {"gid": g2, "action": "resume"})
        chk("站长可一键恢复", s == 200 and js(b).get("ok"), js(b).get("msg"))
        chk("恢复后重新生效", guest.wl_check(g2, FP_A, IP_HOME, DV_A)[0] == "ok")

        print("\n== 5. 申请侧：同一台设备换 gid 再申请也会被挡 ==")
        with guest._lock:
            guest._db["wl_apps"] = []
            guest._db["whitelist"] = []
            guest._save()
        guest.wl_apply("3333333333333333", FP_A, "203.0.113.77", "", "ua", "第一次申请白名单", DV_A)
        r = guest.wl_apply("4444444444444444", FP_A, "203.0.113.78", "", "ua",
                           "换 gid 再来一次试试", DV_A)
        chk("同设备密钥的第二次申请被拒", not r.get("ok"), r.get("msg"))

        # ============ B. 公网暴露面 ============
        print("\n== 6. 公网只认访客页（fail-closed） ==")
        wl_reset()
        st, hd, body = pub_get("/guest")
        chk("公网能打开访客页", st == 200 and b"RVC" in body, st)
        st, _h, body = pub_get("/api/guest/models")
        chk("公网能读声线列表", st == 200, st)
        st, _h, body = pub_get("/panel")
        chk("公网打不开监控面板", st == 403, st)
        st, _h, body = pub_get("/api/admin/requests")
        chk("公网读不到审批列表", st == 403, st)
        st, _h, body = pub_get("/api/status")
        chk("公网读不到服务状态", st == 403, st)
        st, _h, body = pub_get("/.env")
        chk("公网试 /.env 被拒", st in (403, 404), st)
        st, _h, body = pub_post("/api/update/apply", {})
        chk("公网不可能触发一键更新", st == 403, st)
        st, _h, body = pub_get("/api/guest/../admin/requests")
        chk("路径穿越被拒", st in (400, 403, 404), st)
        sv = panel.security_view()
        chk("探测被记进统计", sv.get("probe_total", 0) >= 4, sv.get("probe_total"))
        chk("最近探测里有路径记录", bool(sv.get("recent")), (sv.get("recent") or [])[:2])

        print("\n== 7. Host 头校验（防 DNS rebinding） ==")
        st, _h, _b = req("/api/status", headers={"Host": "evil.example.com"})
        chk("Host 不是本机 → 不算本机管理", st == 403, st)
        st, _h, body = req("/", headers={"Host": "evil.example.com"})
        head = body.decode("utf-8", "ignore")
        chk("伪造 Host 打开首页只给访客页（不给控制台）",
            "secState" not in head and "访客" in head or st == 403, (st, len(head)))
        st, _h, _b = req("/api/status")
        chk("本机正常 Host 仍可用", st == 200, st)

        print("\n== 8. 连续探测 → 临时封禁 ==")
        bad = {"CF-Connecting-IP": "203.0.113.99"}
        codes = []
        for i in range(panel.PROBE_LIMIT + 3):
            st, _h, _b = req("/admin-probe-%d" % i, headers=bad)
            codes.append(st)
        chk("探测请求一律 403", all(c == 403 for c in codes), codes[:5])
        st, _h, body = req("/guest", headers=bad)
        chk("被封后连访客页也进不来", st == 403, st)
        chk("提示是临时限制", "限制" in js(body).get("msg", ""), js(body).get("msg"))
        sv = panel.security_view()
        chk("封禁列表里有这个 IP",
            any(b["ip"] == "203.0.113.99" for b in sv.get("banned") or []), sv.get("banned"))
        chk("别的 IP 不受影响", pub_get("/guest")[0] == 200)
        s, _h, b = req("/api/admin/security", "POST", {"act": "unban", "ip": "203.0.113.99"})
        chk("站长可以解除误封", s == 200 and js(b).get("ok"), js(b).get("msg"))
        chk("解除后恢复访问", req("/guest", headers=bad)[0] == 200)

        print("\n== 9. 一键暂停公网访问 ==")
        s, _h, b = req("/api/admin/security", "POST", {"act": "pause"})
        chk("本机能暂停公网访问", s == 200 and js(b).get("ok"), js(b).get("msg"))
        st, _h, body = pub_get("/")
        chk("公网打开只剩维护页", st == 503 and "维护" in body.decode("utf-8", "ignore"), st)
        st, _h, body = pub_get("/api/guest/models")
        chk("公网接口也停", st == 503, st)
        st, _h, body = req("/api/status")
        chk("本机控制台不受影响", st == 200, st)
        st, _h, body = req("/")
        chk("本机仍能看控制台", st == 200 and "安全" in body.decode("utf-8", "ignore"), st)
        s, _h, b = req("/api/admin/security", "POST", {"act": "resume"})
        chk("恢复公网访问", s == 200 and js(b).get("ok"), js(b).get("msg"))
        chk("恢复后公网可访问", pub_get("/guest")[0] == 200)

        print("\n== 10. 页面与控制台 ==")
        st, hd, html = req("/")
        chk("控制台含安全卡片", "secState" in html.decode("utf-8", "ignore"))
        chk("CSP 仍生效", "Content-Security-Policy" in hd)
        st, hd, html = pub_get("/guest")
        txt = html.decode("utf-8", "ignore")
        chk("访客页会上报设备密钥", "deviceKey" in txt and "rvc_dv" in txt)
        chk("访客页有被暂停的提示分支", "suspended" in txt)

        print("\n== 11. 端口独占（防重复启动静默叠加） ==")
        # 曾经踩过：allow_reuse_address=True 时 Windows 允许抢占已在用的端口，
        # 重复双击启动脚本会叠出多个实例，请求随机落到旧实例上，
        # 表现为同一个地址一会儿 200 一会儿 404（新增页面「看不见」）。
        s_a = panel.PanelServer(("127.0.0.1", 8892), panel.Handler)
        try:
            s_b = panel.PanelServer(("127.0.0.1", 8892), panel.Handler)
            chk("重复启动被拒绝", False, "第二个实例也绑上了 8892")
            s_b.server_close()
        except OSError as e:
            chk("重复启动被拒绝", getattr(e, "winerror", 0) == 10048 or "addr" in str(e).lower(), e)
        finally:
            s_a.server_close()

    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        shutil.rmtree(TMP, ignore_errors=True)
        print("\n" + "=" * 52)
        print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项：")
            for f in FAIL:
                print("  - " + f)
            sys.exit(1)


if __name__ == "__main__":
    main()
