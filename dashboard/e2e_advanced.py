# -*- coding: utf-8 -*-
"""高级设置回归测试：同时制作数量（并发）+ 翻唱参数下发 + 控制台接口。

覆盖：
    1. settings 模块：默认值 / 合并保存 / 越界夹取 / 非法枚举回落 / 落盘 / 恢复默认
    2. 并发闸 _acquire_slot / _release_slot：上限内立即进、超了阻塞、还位后放行
    3. 队列调度：cap=2 时批准三单 → 两单同时在跑，第三单排队；调大上限立刻多开
    4. 翻唱参数真的进了 /convert 的 predict（用假 gradio_client 抓 kwargs）
    5. HTTP：GET /api/admin/settings、POST 保存 / 恢复默认 / 非法值夹取

用 8896 起临时实例（不碰 8899），设置写到临时文件，跑完自动恢复 guest_db.json。
    python e2e_advanced.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8896"
os.environ["RVC_NO_AUTOPUMP"] = "1"          # 测试实例不要自动跑真任务

TMP = tempfile.mkdtemp(prefix="rvc_adv_")
os.environ["RVC_SETTINGS_PATH"] = os.path.join(TMP, "advanced_settings.json")
os.environ["RVC_GUEST_DB"] = os.path.join(TMP, "guest_db.json")   # 不碰正在跑的面板的库

import panel      # noqa: E402
import guest      # noqa: E402
import cover      # noqa: E402
import settings   # noqa: E402

BASE = "http://127.0.0.1:8896"
DB = guest.DB_PATH

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


def count_running():
    with guest._lock:
        return sum(1 for x in guest._db["requests"] if x.get("state") == "running")


def wait_until(fn, timeout=12, gap=0.3):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(gap)
    return False


def main():
    real_run = cover._run_cover
    real_covers, real_index, real_temp = cover.COVERS_DIR, cover.INDEX_PATH, cover.BACKEND_TEMP
    work = os.path.join(TMP, "covers")
    os.makedirs(work, exist_ok=True)
    cover.COVERS_DIR = work
    cover.INDEX_PATH = os.path.join(work, "index.json")
    cover.BACKEND_TEMP = work

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8896), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    try:
        print("\n== 1. 设置模块：默认值 / 校验 / 落盘 ==")
        d = settings.get()
        chk("默认同时制作 1 单", d.get("max_parallel") == 1, d.get("max_parallel"))
        chk("默认参数齐全", (d.get("f0_method") == "rmvpe" and d.get("index_rate") == 0.75
                            and d.get("reverb_intensity") == 4.0 and d.get("uvr5_agg") == 10),
            d)

        r1 = settings.save({"max_parallel": 2, "f0_method": "harvest"})
        chk("保存部分字段成功", r1.get("ok"), r1.get("msg"))
        now = settings.get()
        chk("改的字段生效", now.get("max_parallel") == 2 and now.get("f0_method") == "harvest", now)
        chk("没改的字段保持默认", now.get("index_rate") == 0.75 and now.get("uvr5_agg") == 10, now)
        chk("落盘文件存在", os.path.isfile(settings.PATH), settings.PATH)

        r2 = settings.save({"max_parallel": 99})
        chk("超上限被夹到 6", settings.get().get("max_parallel") == 6 and r2.get("warns"),
            (settings.get().get("max_parallel"), r2.get("warns")))

        r3 = settings.save({"index_rate": 9.9, "uvr5_agg": -5, "filter_radius": 100})
        v = settings.get()
        chk("浮点越界夹到 1.0", v.get("index_rate") == 1.0, v.get("index_rate"))
        chk("整数下限夹到 0", v.get("uvr5_agg") == 0, v.get("uvr5_agg"))
        chk("整数上限夹到 7", v.get("filter_radius") == 7, v.get("filter_radius"))

        r4 = settings.save({"f0_method": "不存在的算法"})
        chk("非法枚举回落默认", settings.get().get("f0_method") == "rmvpe" and r4.get("warns"),
            (settings.get().get("f0_method"), r4.get("warns")))

        r5 = settings.save({"hack": 1, "drop": "table"})
        chk("没有合法字段时拒绝且不改盘", not r5.get("ok"), r5.get("msg"))

        settings.save({"vocal_vol": "3.5"})
        chk("字符串数字也能收", settings.get().get("vocal_vol") == 3.5, settings.get().get("vocal_vol"))

        settings.reset()
        chk("恢复默认全回到初始值", settings.get().get("max_parallel") == 1
            and settings.get().get("index_rate") == 0.75, settings.get())

        print("\n== 2. 并发闸：占位 / 还位 ==")
        settings.save({"max_parallel": 2})
        cover._slots_used = 0
        acquired = []
        lock = threading.Lock()

        def take(tag):
            cover._acquire_slot("adv-" + tag)
            with lock:
                acquired.append((tag, time.time()))

        t1 = threading.Thread(target=take, args=("a",), daemon=True)
        t2 = threading.Thread(target=take, args=("b",), daemon=True)
        t3 = threading.Thread(target=take, args=("c",), daemon=True)
        for t in (t1, t2, t3):
            t.start()
        time.sleep(0.9)
        chk("上限 2 时前两单立即占到位", cover.slots_used() == 2 and len(acquired) == 2,
            (cover.slots_used(), len(acquired)))
        chk("第三单被挡住等着", t3.is_alive() and len(acquired) == 2, len(acquired))
        cover._release_slot()
        t3.join(timeout=4)
        chk("还一个位后第三单放行", (not t3.is_alive()) and cover.slots_used() == 2,
            (t3.is_alive(), cover.slots_used()))
        cover._release_slot()
        cover._release_slot()
        chk("全部还位归零", cover.slots_used() == 0, cover.slots_used())

        print("\n== 3. 队列并发：cap=2 时同时跑两单 ==")
        cover._slots_used = 0
        settings.save({"max_parallel": 2})
        model = (cover.models().get("models") or [{}])[0].get("name")
        if not model:
            chk("硬盘上有可用声线", False)
            return

        peak = {"n": 0}
        stub_lock = threading.Lock()

        def stub(jid, song_id, title, artist, model_, key_shift, acapella, key=None):
            cover._acquire_slot(jid)          # 走真闸，验证池化调度
            try:
                cover._update(jid, state="running", started=time.time(), stage="桩任务")
                with stub_lock:
                    peak["n"] = max(peak["n"], cover.slots_used())
                time.sleep(2.6)
                cover._update(jid, state="done", stage="完成", ended=time.time(),
                              file=None, size_text="桩")
            finally:
                cover._release_slot()
                if key:
                    with cover._lock:
                        if cover._inflight.get(key) == jid:
                            cover._inflight.pop(key, None)

        cover._run_cover = stub

        def new_req(title, sid, ip):
            res = guest.create_request(guest.secrets.token_hex(8), ip, "", "test",
                                       {"type": "cover", "song_id": sid, "title": title,
                                        "artist": "测试歌手", "model": model, "acapella": False})
            return (res.get("request") or {}).get("id")

        a = new_req("并发测试一", "910000001", "10.8.1.1")
        b = new_req("并发测试二", "910000002", "10.8.1.2")
        c = new_req("并发测试三", "910000003", "10.8.1.3")
        chk("三条申请建好", all([a, b, c]), [a, b, c])

        jpost("/api/admin/approve", {"id": a})
        jpost("/api/admin/approve", {"id": b})
        s3, r3 = jpost("/api/admin/approve", {"id": c})
        chk("前两单同时在跑", count_running() == 2, count_running())
        chk("第三单在排队（没抢跑）", state_of(c) == "queued" and r3.get("ok"),
            (state_of(c), r3.get("msg")))

        s4, q = jget("/api/admin/queue")
        chk("队列接口报了并发信息", q.get("cap") == 2 and q.get("running_count") == 2
            and len(q.get("running_all") or []) == 2,
            (q.get("cap"), q.get("running_count"), q.get("running_all")))
        chk("兼容字段 running 仍在", q.get("running") in ("并发测试一", "并发测试二"), q.get("running"))

        settings.save({"max_parallel": 3})
        jpost("/api/admin/pump", {})
        chk("调大上限后立刻多开一单", count_running() == 3 and state_of(c) == "running",
            (count_running(), state_of(c)))
        chk("峰值并发到过 3", wait_until(lambda: peak["n"] >= 3, 5), peak["n"])

        chk("三单都做完", wait_until(lambda: all(state_of(x) == "done" for x in (a, b, c)), 14),
            [state_of(x) for x in (a, b, c)])
        chk("做完后占位归零", wait_until(lambda: cover.slots_used() == 0, 6), cover.slots_used())

        print("\n== 4. 翻唱参数真的传给了后端 ==")
        cover._run_cover = real_run
        settings.save({"max_parallel": 1, "f0_method": "harvest", "index_rate": 0.5,
                       "filter_radius": 5, "reverb_intensity": 6.0, "delay_intensity": 1.5,
                       "uvr5_agg": 14, "vocal_vol": 2.0, "inst_vol": -3.0})
        seen = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def predict(self, **kw):
                seen.append(kw)
                p = os.path.join(work, "fake-out.mp3")
                with open(p, "wb") as f:
                    f.write(b"\xff" * 4096)
                return p

        fake = types.ModuleType("gradio_client")
        fake.Client = FakeClient
        sys.modules["gradio_client"] = fake
        real_port = cover._port_open
        cover._port_open = lambda *a, **k: True
        try:
            jid = cover._new_job(title="参数下发测试", model=model)
            cover._run_cover(jid, "920000001", "参数下发测试", "测试", model, 0, False, None)
            st = cover.job_status(jid) or {}
            chk("假后端被调用一次", len(seen) == 1, len(seen))
            kw = seen[0] if seen else {}
            chk("f0_method 用的是设置值", kw.get("f0_method") == "harvest", kw.get("f0_method"))
            chk("index_rate 以字符串下发", kw.get("index_rate") == "0.5", kw.get("index_rate"))
            chk("filter_radius 生效", kw.get("filter_radius") == 5, kw.get("filter_radius"))
            chk("混响/回声生效", kw.get("reverb_intensity") == 6.0
                and kw.get("delay_intensity") == 1.5,
                (kw.get("reverb_intensity"), kw.get("delay_intensity")))
            chk("分离聚合度生效", kw.get("uvr5_agg") == 14, kw.get("uvr5_agg"))
            chk("人声/伴奏音量生效", kw.get("vocal_vol") == 2.0 and kw.get("inst_vol") == -3.0,
                (kw.get("vocal_vol"), kw.get("inst_vol")))
            chk("任务跑成 done", st.get("state") == "done", st.get("state"))
            chk("成品进了索引", any(i.get("song_id") == "920000001"
                                    for i in cover._load_index()),
                cover._load_index()[:2])
        finally:
            cover._port_open = real_port
            sys.modules.pop("gradio_client", None)

        print("\n== 5. HTTP：读 / 存 / 恢复默认 ==")
        s5, ui = jget("/api/admin/settings")
        chk("GET 能读到设置", s5 == 200 and ui.get("ok") and len(ui.get("fields") or []) == 9,
            (s5, len(ui.get("fields") or [])))
        keys = [f.get("key") for f in ui.get("fields") or []]
        chk("字段含并发与翻唱参数", "max_parallel" in keys and "reverb_intensity" in keys, keys)
        chk("每个字段带说明文案", all(f.get("hint") for f in ui.get("fields") or []))

        s6, r6 = jpost("/api/admin/settings", {"values": {"max_parallel": 4, "f0_method": "crepe"}})
        chk("POST 保存成功", s6 == 200 and r6.get("ok")
            and (r6.get("values") or {}).get("max_parallel") == 4, r6.get("msg"))
        chk("保存后接口读到新值", (jget("/api/admin/settings")[1].get("values") or {})
            .get("f0_method") == "crepe")

        s7, r7 = jpost("/api/admin/settings", {"values": {"max_parallel": 999}})
        chk("越界值经接口也夹取", (r7.get("values") or {}).get("max_parallel") == 6
            and r7.get("warns"), (r7.get("values"), r7.get("warns")))

        s8, r8 = jpost("/api/admin/settings", {"act": "reset"})
        chk("恢复默认可用", s8 == 200 and r8.get("ok")
            and (r8.get("values") or {}).get("max_parallel") == 1, r8.get("msg"))

        s9, r9 = jpost("/api/admin/settings", {"values": {"nope": 1}})
        chk("没有合法字段给出明确提示", s9 == 400 and not r9.get("ok"), (s9, r9.get("msg")))

        s10, r10 = jpost("/api/admin/settings", {"values": {}})
        chk("空补丁不误改设置", not r10.get("ok") and settings.get().get("max_parallel") == 1,
            r10.get("msg"))

        print("\n== 6. 设置文件坏了也不崩 ==")
        with open(settings.PATH, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        settings._cache = None
        bad = settings.get()
        chk("JSON 损坏回落默认", bad.get("max_parallel") == 1 and bad.get("f0_method") == "rmvpe", bad)
        with open(settings.PATH, "w", encoding="utf-8") as f:
            json.dump({"max_parallel": "abc", "f0_method": 123}, f)
        settings._cache = None
        bad2 = settings.get()
        chk("类型乱写也收窄成默认", bad2.get("max_parallel") == 1
            and bad2.get("f0_method") == "rmvpe", bad2)

        print("\n== 7. 公网隧道：UI 一键开启/关闭（不碰真 cloudflared） ==")
        # 假路径注入：start 在「找不到 cloudflared」这一步就该停，不会真开隧道
        os.environ["RVC_CLOUDFLARED"] = os.path.join(TMP, "没有这文件.exe")
        s, r = jpost("/api/action", {"action": "tunnel_start"})
        chk("cloudflared 缺失时报可读错误", s == 200 and not r.get("ok")
            and "cloudflared" in r.get("msg", ""), (s, r.get("msg")))
        if panel.tunnel_pids():
            print("  [跳过] 机器上有真隧道在跑，不动它（stop 用例略过）")
        else:
            s, r = jpost("/api/action", {"action": "tunnel_stop"})
            chk("没在跑时关隧道 ok 且提示未运行", s == 200 and r.get("ok")
                and "未运行" in r.get("msg", ""), (s, r.get("msg")))
        st, pub = jget("/api/public")
        chk("/api/public 返回 url+alive", st == 200 and "url" in pub and "alive" in pub, pub)
        s, r = jpost("/api/action", {"action": "tunnel_start2"})
        chk("未知 action 报错", s == 200 and not r.get("ok"), (s, r.get("msg")))

    finally:
        cover._run_cover = real_run
        cover.COVERS_DIR, cover.INDEX_PATH, cover.BACKEND_TEMP = real_covers, real_index, real_temp
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
        print("\n=== %d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项：" + "；".join(FAIL))


if __name__ == "__main__":
    main()
