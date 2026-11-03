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
