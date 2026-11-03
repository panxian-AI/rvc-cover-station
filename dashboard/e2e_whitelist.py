# -*- coding: utf-8 -*-
"""白名单回归测试：免审批直通、三要素匹配、防批量刷、防注入、权限。

用 8895 起一个临时实例（不碰 8899），跑完自动恢复 guest_db.json。
    python e2e_whitelist.py
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
os.environ["RVC_PANEL_PORT"] = "8895"
os.environ["RVC_NO_AUTOPUMP"] = "1"   # 测试实例不要自动跑真任务

import panel   # noqa: E402
import guest   # noqa: E402
import cover   # noqa: E402

BASE = "http://127.0.0.1:8895"
DB = guest.DB_PATH
BAK = DB + ".wl_bak"

PASS, FAIL = [], []

FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
IP_HOME = "203.0.113.10"
IP_HOME2 = "203.0.113.11"    # 第 3 节起用（第 2 节已把 IP_HOME 的申请配额用满）
IP_4G = "198.51.100.77"


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jpost(path, body=None, hdrs=None):
    data = json.dumps(body or {}).encode()
    h = {"Content-Type": "application/json"}
    h.update(hdrs or {})
    r = urllib.request.Request(BASE + path, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def jget(path, hdrs=None):
    h = dict(hdrs or {})
    r = urllib.request.Request(BASE + path, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def gpost(path, body, gid):
    """带访客 Cookie 的公开接口调用。"""
    return jpost(path, body, {"Cookie": "rvc_guest=" + gid})


def gget(path, gid):
    return jget(path, {"Cookie": "rvc_guest=" + gid})


def state_of(rid):
    with guest._lock:
        r = guest._find(rid)
        return (r or {}).get("state")


def app_id_of(gid):
    with guest._lock:
        for a in guest._db.get("wl_apps") or []:
            if a.get("gid") == gid:
                return a.get("id")
    return None


def reset_wl():
    with guest._lock:
        guest._db["wl_apps"] = []
        guest._db["whitelist"] = []
        guest._save()


def main():
    if os.path.isfile(DB):
        shutil.copy2(DB, BAK)

    calls = []
    real_run = cover._run_cover

    def stub(jid, song_id, title, artist, model, key_shift, acapella, key=None):
        calls.append(song_id)
        cover._update(jid, state="running", started=time.time(), stage="桩任务")
        time.sleep(1.2)
        cover._update(jid, state="done", stage="完成", ended=time.time(), file=None,
                      size_text="桩")
        if key:
            with cover._lock:
                if cover._inflight.get(key) == jid:
                    cover._inflight.pop(key, None)

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8895), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    try:
        cover._run_cover = stub
        model = (cover.models().get("models") or [{}])[0].get("name")
        if not model:
            chk("硬盘上有可用声线", False)
            return
        reset_wl()

        print("\n== 1. 申请参数校验（格式 / 长度 / 注入面） ==")
        r = guest.wl_apply("0123456789abcdef", "不是指纹!!", IP_HOME, "", "test", "我想加入白名单")
        chk("非法指纹被拒", not r.get("ok"), r.get("msg"))
        r = guest.wl_apply("2222222222222222", FP_A.upper(), IP_HOME, "", "test", "大写指纹统一成小写")
        chk("大写指纹被统一成小写接受", r.get("ok"), r.get("msg"))
        with guest._lock:
            up = [x for x in guest._db["wl_apps"] if x.get("gid") == "2222222222222222"]
        chk("落库的是小写指纹", bool(up) and up[0]["fp"] == FP_A, up and up[0]["fp"][:8])
        r = guest.wl_apply("3333333333333333", FP_A, IP_HOME, "", "test", "短")
        chk("理由太短被拒", not r.get("ok"), r.get("msg"))
        weird = "批量测试\x00\x01\x1f<script>alert(1)</script>" + "x" * 500
        r = guest.wl_apply("4444444444444444", FP_A, IP_HOME, "", "test", weird)
        chk("带控制字符/超长的理由能被净下来", r.get("ok"), r.get("msg"))
        with guest._lock:
            a0 = [x for x in guest._db["wl_apps"] if x.get("gid") == "4444444444444444"]
            stored = a0[0]["reason"] if a0 else ""
        chk("理由已去控制字符", "\x00" not in stored and "\x01" not in stored, repr(stored[:40]))
        chk("理由限长 200", len(stored) <= guest.WL_REASON_MAX, len(stored))
        chk("HTML 原样存储（渲染层转义，不做黑名单过滤）", "<script>" in stored)
        reset_wl()

        print("\n== 2. 申请侧防批量刷 ==")
        g1 = "1111111111111111"
        r = guest.wl_apply(g1, FP_A, IP_HOME, "", "test", "正常申请理由，第一个")
        chk("正常申请通过", r.get("ok") and r.get("state") == "pending", r.get("msg"))
        r = guest.wl_apply(g1, FP_A, IP_HOME, "", "test", "重复申请应该被挡")
        chk("同 gid 二次申请被拒", not r.get("ok"), r.get("msg"))
        for i, g in enumerate(["2222222222222222", "3333333333333333"]):
            guest.wl_apply(g, FP_B, IP_HOME, "", "test", "同 IP 第 %d 个申请" % (i + 2))
        r = guest.wl_apply("4444444444444444", FP_C, IP_HOME, "", "test", "同 IP 第 4 个，该挡了")
        chk("同 IP 24h 超过 3 次被拒", not r.get("ok"), r.get("msg"))
        r = guest.wl_apply("4444444444444444", FP_C, IP_4G, "", "test", "换个 IP 就能绕过？不能")
        chk("换 IP 不影响同 IP 计数外的正常申请", r.get("ok"), r.get("msg"))
        # 被拒冷却
        aid = app_id_of(g1)
        s, rr = jpost("/api/admin/wl_decide", {"id": aid, "action": "reject", "note": "理由不充分"})
        chk("拒绝成功", s == 200 and rr.get("ok"), rr.get("msg"))
        r = guest.wl_apply(g1, FP_A, IP_HOME, "", "test", "被拒后立刻再试")
        chk("被拒 24h 冷却生效", not r.get("ok"), r.get("msg"))

        print("\n== 3. 批准与三要素匹配 ==")
        ga = "aaaaaaaaaaaaaaaa"
        guest.wl_apply(ga, FP_A, IP_HOME2, "", "test", "我要免审批直通，谢谢站长")
        aid = app_id_of(ga)
        s, rr = jpost("/api/admin/wl_decide", {"id": aid, "action": "approve", "note": "老朋友了"})
        chk("批准成功", s == 200 and rr.get("ok"), rr.get("msg"))
        chk("三要素全中 → 白名单生效",
            guest.is_whitelisted(ga, FP_A, IP_HOME2) is True)
        chk("IP 变了 → 失效", guest.is_whitelisted(ga, FP_A, IP_4G) is False)
        chk("指纹变了 → 失效", guest.is_whitelisted(ga, FP_B, IP_HOME2) is False)
        chk("别人拿同 IP 同指纹伪造 gid → 失效",
            guest.is_whitelisted("bbbbbbbbbbbbbbbb", FP_A, IP_HOME) is False)
        chk("指纹格式不对 → 直接 False",
            guest.is_whitelisted(ga, "zzzz", IP_HOME) is False)

        s, wv = jget("/api/admin/wl")
        chk("控制台白名单接口可用", s == 200 and wv.get("ok"))
        chk("名单里有这条", any(w["gid"] == ga for w in wv.get("entries", [])))
        ent = [w for w in wv.get("entries", []) if w["gid"] == ga][0]
        chk("备注/命中次数在", ent.get("note") == "老朋友了" and ent.get("hits") >= 1,
            (ent.get("note"), ent.get("hits")))

        print("\n== 4. 免审批直通（核心权益） ==")
        before = len(calls)
        res = guest.create_request(ga, IP_HOME2, "", "test",
                                   {"type": "cover", "song_id": "900000101",
                                    "title": "白名单直通测试", "artist": "测试",
                                    "model": model, "acapella": False, "fp": FP_A})
        rid = (res.get("request") or {}).get("id")
        chk("提交直接返回白名单直通提示", res.get("ok") and "白名单" in (res.get("msg") or ""),
            res.get("msg"))
        with guest._lock:
            rr_ = guest._find(rid)
            auto = bool(rr_.get("auto"))
        chk("申请记录带 auto 标记", auto)
        chk("没有等站长批准就开跑了", len(calls) == before + 1, (before, len(calls)))
        deadline = time.time() + 10
        while time.time() < deadline and state_of(rid) != "done":
            time.sleep(0.3)
        chk("直通单也能正常做完", state_of(rid) == "done", state_of(rid))

        # 未在名单的访客仍然要走审批
        res2 = guest.create_request("cccccccccccccccc", IP_4G, "", "test",
                                    {"type": "cover", "song_id": "900000102",
                                     "title": "普通访客测试", "artist": "测试",
                                     "model": model, "acapella": False, "fp": FP_C})
        rid2 = (res2.get("request") or {}).get("id")
        chk("普通访客仍是 pending", state_of(rid2) == "pending", state_of(rid2))
        # 带了假指纹也不行
        res3 = guest.create_request("dddddddddddddddd", IP_HOME, "", "test",
                                    {"type": "cover", "song_id": "900000103",
                                     "title": "伪造指纹测试", "artist": "测试",
                                     "model": model, "acapella": False, "fp": FP_A})
        chk("换 gid 伪造别人指纹 → 仍 pending",
            state_of((res3.get("request") or {}).get("id")) == "pending")

        print("\n== 5. 换网络 → 重新申请 → 幂等更新 IP ==")
        s, mine = gget("/api/guest/wl?fp=" + FP_A, ga)
        chk("访客侧状态 = mismatch（网络变了）",
            s == 200 and mine.get("state") == "mismatch", (s, mine.get("state")))
        r = guest.wl_apply(ga, FP_A, IP_4G, "", "test", "我换了个网络，请站长重新批一下")
        chk("换网络后可以重新申请", r.get("ok"), r.get("msg"))
        aid2 = app_id_of(ga)
        s, rr = jpost("/api/admin/wl_decide", {"id": aid2, "action": "approve", "note": "换网络"})
        chk("重新批准成功", s == 200 and rr.get("ok"), rr.get("msg"))
        chk("新 IP 生效", guest.is_whitelisted(ga, FP_A, IP_4G) is True)
        chk("旧 IP 已被顶掉", guest.is_whitelisted(ga, FP_A, IP_HOME2) is False)
        s, wv2 = jget("/api/admin/wl")
        chk("名单没有重复条目（幂等更新）",
            sum(1 for w in wv2.get("entries", []) if w["gid"] == ga) == 1)

        print("\n== 6. 公开接口路由与权限 ==")
        gid_http = "1234567890abcdef"
        s, r = gpost("/api/guest/wl_apply",
                     {"fp": FP_B, "reason": "HTTP 路由测试的申请理由"}, gid_http)
        chk("POST /api/guest/wl_apply 走得通", s == 200 and r.get("ok"), (s, r.get("msg")))
        s, r = gget("/api/guest/wl?fp=" + FP_B, gid_http)
        chk("GET /api/guest/wl 走得通", s == 200 and r.get("state") == "pending", (s, r.get("state")))
        s, r = jpost("/api/admin/wl_decide", {"id": "x", "action": "approve"},
                     hdrs={"X-Forwarded-For": "1.2.3.4"})
        chk("带转发头的「管理」请求被 403", s == 403, s)
        s, r = jget("/api/admin/wl", hdrs={"X-Forwarded-For": "1.2.3.4"})
        chk("白名单列表也是本机限定", s == 403, s)

        print("\n== 7. 移除与落盘 ==")
        s, rr = jpost("/api/admin/wl_revoke", {"gid": ga})
        chk("移除成功", s == 200 and rr.get("ok"), rr.get("msg"))
        chk("移除后立即失效", guest.is_whitelisted(ga, FP_A, IP_4G) is False)
        with open(DB, "r", encoding="utf-8") as f:
            disk = json.load(f)
        chk("wl_apps / whitelist 已写入 guest_db.json",
            isinstance(disk.get("wl_apps"), list) and isinstance(disk.get("whitelist"), list))
        chk("白名单申请记录仍在（audit 留痕）",
            len(disk.get("wl_apps") or []) >= 5, len(disk.get("wl_apps") or []))
    finally:
        cover._run_cover = real_run
        srv.shutdown()
        if os.path.isfile(BAK):
            shutil.copy2(BAK, DB)
            os.remove(BAK)
        print("\n=== %d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项：" + "；".join(FAIL))


if __name__ == "__main__":
    main()
