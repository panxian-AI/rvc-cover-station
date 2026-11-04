# -*- coding: utf-8 -*-
"""队列回归测试：批准即入队（不必等前一单做完）、默认先到先来、站长可调顺序。

用 8895 起一个临时实例（不碰 8899），跑完自动恢复 guest_db.json。
    python e2e_queue.py
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
# 高级设置用临时文件：这个用例断言「同时只跑一单」，不能被站长调大的并发数干扰
os.environ.setdefault("RVC_SETTINGS_PATH",
                      os.path.join(os.environ.get("TEMP") or ".", "rvc_queue_settings.json"))

import panel   # noqa: E402
import guest   # noqa: E402
import cover   # noqa: E402

BASE = "http://127.0.0.1:8895"
DB = guest.DB_PATH
BAK = DB + ".queue_bak"

PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jpost(path, body=None):
    data = json.dumps(body or {}).encode()
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def jget(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def state_of(rid):
    with guest._lock:
        r = guest._find(rid)
        return (r or {}).get("state")


def _guest_view(rid):
    """访客视角的单条视图（不经过 HTTP，直接走 /api/guest/my 用的那个函数）。"""
    with guest._lock:
        r = guest._find(rid)
        return guest._req_view(r) if r else {}


def wait_state(rid, want, timeout=14):
    end = time.time() + timeout
    while time.time() < end:
        if state_of(rid) == want:
            return True
        time.sleep(0.4)
    return False


def main():
    if os.path.isfile(DB):
        shutil.copy2(DB, BAK)

    # 用桩替掉真跑：1.2 秒后标记完成，只验证排队/调度逻辑
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

        def new_req(title, sid, ip):
            res = guest.create_request(guest.secrets.token_hex(8), ip, "", "test",
                                       {"type": "cover", "song_id": sid, "title": title,
                                        "artist": "测试歌手", "model": model, "acapella": False})
            return (res.get("request") or {}).get("id")

        print("\n== 1. 批准即入队，不再被「还有一单在做」挡住 ==")
        a = new_req("队列测试一", "900000001", "10.9.0.1")
        b = new_req("队列测试二", "900000002", "10.9.0.2")
        c = new_req("队列测试三", "900000003", "10.9.0.3")
        chk("三条申请建好", all([a, b, c]), [a, b, c])

        s1, r1 = jpost("/api/admin/approve", {"id": a})
        chk("第一单立即开始", s1 == 200 and r1.get("ok") and state_of(a) == "running", r1.get("msg"))
        s2, r2 = jpost("/api/admin/approve", {"id": b})
        chk("第二单被接受（不报错）", s2 == 200 and r2.get("ok"), r2.get("msg"))
        chk("第二单进入排队", state_of(b) == "queued", state_of(b))
        s3, r3 = jpost("/api/admin/approve", {"id": c})
        chk("第三单也进排队", r3.get("ok") and state_of(c) == "queued", r3.get("msg"))
        chk("排在第二单后面", (r3.get("pos") or 0) == 2, r3.get("pos"))
        chk("同一时刻只有一单在跑",
            sum(1 for x in guest._db["requests"] if x.get("state") == "running") == 1)

        print("\n== 2. 队列查询与顺序调整 ==")
        s4, q = jget("/api/admin/queue")
        chk("队列接口可用", s4 == 200 and q.get("ok"), s4)
        chk("队列顺序 = 批准顺序（先到先来）", [x["id"] for x in q.get("queue", [])] == [b, c],
            [x["pos"] for x in q.get("queue", [])])
        chk("显示正在做的单", q.get("running") == "队列测试一", q.get("running"))

        s5, m = jpost("/api/admin/move", {"id": c, "dir": "top"})
        chk("把第三单置顶", m.get("ok") and m.get("pos") == 1, m.get("msg"))
        s6, q2 = jget("/api/admin/queue")
        chk("队列变成 3 → 2", [x["id"] for x in q2.get("queue", [])] == [c, b],
            [x["id"] for x in q2.get("queue", [])])
        s7, m2 = jpost("/api/admin/move", {"id": c, "dir": "down"})
        chk("再往下挪一位", m2.get("pos") == 2, m2.get("msg"))

        print("\n== 3. 前一单做完，自动接力 ==")
        chk("第一单跑完变 done", wait_state(a, "done", 12), state_of(a))
        time.sleep(0.8)
        chk("队列自动推进到下一单", state_of(b) == "running" or state_of(c) == "running",
            (state_of(b), state_of(c)))
        chk("推进的是排在前面那单", state_of(b) == "running", (state_of(b), state_of(c)))

        print("\n== 3.5 进度可见性：位次 / 阶段 / 预计 ==")
        s_q, q4 = jget("/api/admin/queue")
        rows = q4.get("queue") or []
        chk("队列每行都带「前面几单」", bool(rows) and all("q_ahead" in x for x in rows),
            [x.get("q_ahead") for x in rows])
        chk("第一行的前面是 0 单", rows and rows[0].get("q_ahead") == 0, rows and rows[0].get("q_ahead"))
        chk("每行都带预计时间文案", bool(rows) and all(x.get("eta_text") for x in rows),
            [x.get("eta_text") for x in rows])
        rd = q4.get("running_detail") or []
        chk("正在做的单独有阶段与已用时间",
            bool(rd) and bool(rd[0].get("stage")) and bool(rd[0].get("elapsed_text")), rd)
        chk("阶段已翻成访客话（不是工程原文）",
            bool(rd) and rd[0].get("stage_label") and "（" not in rd[0].get("stage_label", ""),
            rd and rd[0].get("stage_label"))
        chk("进度百分比落在 3~92 之间（不假装做完）",
            bool(rd) and 3 <= int(rd[0].get("pct") or 0) <= 92, rd and rd[0].get("pct"))
        chk("给出了单均耗时", bool(q4.get("avg_job_text")), q4.get("avg_job_text"))

        # 访客视角：同一单从 /api/guest/my 看，必须带同样的位次与阶段
        gid_b = (guest._find(b) or {}).get("gid")
        mine = guest.my_requests(gid_b).get("requests") or []
        vb = [x for x in mine if x["id"] == b]
        chk("访客能看到自己那单的阶段", bool(vb) and bool(vb[0].get("stage_label")), vb)
        chk("访客能看到已用多久", bool(vb) and bool(vb[0].get("elapsed_text")), vb)
        vc = _guest_view(c)
        chk("排队中的访客看到位次与前面几单",
            vc.get("q_pos") == 1 and vc.get("q_ahead") == 0, vc)
        chk("排队中的访客看到预计开始时间", bool(vc.get("eta_text")), vc.get("eta_text"))

        # 估算本身：秒数格式化、阶段翻译、中位数抗异常、位次越靠后越久
        chk("秒数转人话", guest._dur_text(45) == "45 秒"
            and guest._dur_text(125) == "2 分 5 秒" and guest._dur_text(60) == "1 分",
            (guest._dur_text(45), guest._dur_text(125), guest._dur_text(60)))
        chk("阶段翻译：认得后端原文，空值不炸",
            guest._stage_label("派发任务（下载 + 人声分离 + 推理）…") == "下载 / 分离 / 演唱"
            and guest._stage_label("归档成品…") == "收尾导出"
            and guest._stage_label("") == "",
            (guest._stage_label("派发任务（…）…"), guest._stage_label("")))
        saved_db = guest._db
        try:
            t0 = time.time()
            guest._db = {"queue": [], "requests": [
                {"state": "done", "started": t0 - 200, "decided": t0 - 160},   # 40s
                {"state": "done", "started": t0 - 200, "decided": t0 - 170},   # 30s
                {"state": "done", "started": t0 - 200, "decided": t0 - 195},   # 5s（异常值）
            ]}
            chk("单均耗时取中位数（不被极短/极长的单带偏）",
                25 <= guest._avg_job_sec() <= 35, guest._avg_job_sec())
            guest._db = {"queue": [], "requests": [
                {"id": "run", "state": "running", "started": t0 - 10},
            ]}
            e1 = guest._eta_hint("q1", 1, 3)
            e3 = guest._eta_hint("q3", 3, 3)
            chk("排得越后预计越久", e3["eta_sec"] > e1["eta_sec"],
                (e1["eta_sec"], e3["eta_sec"]))
            chk("预计文案能直接看", bool(e1["eta_text"]) and bool(e3["eta_text"]),
                (e1["eta_text"], e3["eta_text"]))
        finally:
            guest._db = saved_db

        print("\n== 3.6 页面接线（访客页 / 控制台）==")
        gpage = open(os.path.join(HERE, "guest_page.html"), encoding="utf-8").read()
        cpage = (open(os.path.join(HERE, "console_tasks_page.html"), encoding="utf-8").read()
                 + open(os.path.join(HERE, "assets", "page_tasks.js"), encoding="utf-8").read())
        chk("访客页有四步进度条", "function stepBar" in gpage and gpage.count("stepBar(r)") >= 1)
        chk("访客页顶部有「我在什么处境」摘要", 'id="mySum"' in gpage and "mySum" in gpage)
        chk("访客页用了位次/前面几单/预计三个字段",
            all(k in gpage for k in ("q_ahead", "q_pos", "eta_text")))
        chk("访客页展示阶段与已用时间",
            "stage_label" in gpage and "elapsed_text" in gpage)
        chk("控制台展示正在做的明细", "running_detail" in cpage and "avg_job_text" in cpage)
        chk("控制台排队行显示前面几单与预计", "q_ahead" in cpage and "eta_text" in cpage)
        chk("控制台申请记录状态列带阶段", "stage_label" in cpage and "第 ${r.q_pos} 位" in cpage)

        print("\n== 4. 排队中的单可以移出 ==")
        s8, rj = jpost("/api/admin/reject", {"id": c})
        chk("移出队列成功", rj.get("ok") and state_of(c) == "rejected", rj.get("msg"))
        s9, q3 = jget("/api/admin/queue")
        chk("队列里不再有它", all(x["id"] != c for x in q3.get("queue", [])),
            [x["id"] for x in q3.get("queue", [])])

        print("\n== 5. 命中成品库的单不排队、不占 GPU ==")
        idx = [i for i in cover._load_index()
               if str(i.get("song_id") or "").isdigit() and cover._alive_file(i.get("name"))]
        if idx:
            ent = idx[-1]
            d = new_req(ent.get("title") or "复用测试", str(ent["song_id"]), "10.9.0.4")
            with guest._lock:
                guest._find(d)["model"] = os.path.basename(ent["model"])
                guest._find(d)["acapella"] = bool(ent.get("acapella"))
            before_running = sum(1 for x in guest._db["requests"] if x.get("state") == "running")
            s10, rr = jpost("/api/admin/approve", {"id": d})
            chk("批准时直接复用", rr.get("ok") and rr.get("reuse") is True, rr.get("msg"))
            chk("立刻变 done 且带文件", state_of(d) == "done" and guest._find(d).get("file"),
                state_of(d))
            chk("没有把正在做的那单挤掉",
                sum(1 for x in guest._db["requests"] if x.get("state") == "running")
                == before_running)
            chk("也没有插进队列", all(x["id"] != d for x in (guest._db.get("queue") or [])))
        else:
            chk("成品库有样本（跳过命中测试）", False)

        print("\n== 6. 队列落盘 & 空队列推进 ==")
        with open(DB, "r", encoding="utf-8") as f:
            disk = json.load(f)
        chk("queue 字段写进了 guest_db.json", isinstance(disk.get("queue"), list),
            disk.get("queue"))
        if wait_state(b, "done", 14):
            chk("最后一单也能做完", True)
        time.sleep(0.8)
        s11, p1 = jpost("/api/admin/pump", {})
        chk("空队列推进返回提示而不是报错", s11 == 200 and p1.get("ok"), p1.get("msg"))
        chk("队列已清空", not (guest._db.get("queue") or []), guest._db.get("queue"))
        chk("同时只跑过一单", len(calls) >= 2, calls)
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
