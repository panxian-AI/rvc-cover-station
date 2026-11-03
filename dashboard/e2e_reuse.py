# -*- coding: utf-8 -*-
"""成品复用回归测试：同样参数的歌 / 朗读，第二次必须直接复用，不再跑一遍。

用 8897 起一个临时实例（不碰 8899），跑完自动恢复 guest_db.json。
    python e2e_reuse.py
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
os.environ["RVC_PANEL_PORT"] = "8897"
os.environ["RVC_NO_AUTOPUMP"] = "1"   # 测试实例不要自动跑真任务

import panel   # noqa: E402
import guest   # noqa: E402
import cover   # noqa: E402
import tts     # noqa: E402

BASE = "http://127.0.0.1:8897"
DB = guest.DB_PATH
BAK = DB + ".reuse_bak"

PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


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


def jget(path, **kw):
    code, _h, raw = req(path, **kw)
    try:
        return code, json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return code, {}


def mp3_set():
    return {f for f in os.listdir(cover.COVERS_DIR) if f.lower().endswith(".mp3")}


def main():
    if os.path.isfile(DB):
        shutil.copy2(DB, BAK)

    # 把「真跑一遍」换成秒睡桩：只验证会不会重复派发，不碰 GPU / 后端 3333
    calls = []
    real_run = cover._run_cover

    def stub(jid, song_id, title, artist, model, key_shift, acapella, key=None):
        calls.append(jid)
        cover._update(jid, state="running", started=time.time(), stage="桩任务")
        time.sleep(2.5)
        cover._update(jid, state="error", stage="失败", ended=time.time(), error="桩任务不真跑")
        if key:
            with cover._lock:
                if cover._inflight.get(key) == jid:
                    cover._inflight.pop(key, None)

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8897), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    try:
        cover._run_cover = stub

        # 从成品库里挑一个真实条目当「做过的那首」
        idx = [i for i in cover._load_index()
               if str(i.get("song_id") or "").isdigit()
               and cover._alive_file(i.get("name"))]
        if not idx:
            chk("成品库里有可用样本", False, "covers/index.json 里没有有效翻唱")
            return
        ent = idx[-1]
        sid, model = str(ent["song_id"]), os.path.basename(ent["model"])
        ks = int(ent.get("key_shift") or 0)
        ac = bool(ent.get("acapella"))

        print("\n== 1. 成品库查询（cover.find_cached） ==")
        hit = cover.find_cached(sid, model, ks, ac)
        chk("同歌+同声线+同调+同清唱 → 命中", bool(hit), hit and hit["name"])
        chk("命中返回的文件真的在硬盘上", bool(hit) and cover._alive_file(hit["name"]) > 0)
        chk("换声线 → 不命中", cover.find_cached(sid, model + ".bak", ks, ac) is None)
        chk("升降调不同 → 不命中", cover.find_cached(sid, model, ks + 2, ac) is None)
        chk("清唱与否不同 → 不命中", cover.find_cached(sid, model, ks, not ac) is None)
        chk("其它歌 → 不命中", cover.find_cached("1", model, ks, ac) is None)

        print("\n== 2. /api/cover/start 直接复用（不产生新文件） ==")
        before = mp3_set()
        code, d = jget("/api/cover/start", method="POST",
                       body={"song_id": sid, "title": ent.get("title"), "artist": ent.get("artist"),
                             "model": model, "key_shift": ks, "acapella": ac})
        chk("接口 200", code == 200, code)
        chk("标记 cached=True", d.get("cached") is True, d.get("msg"))
        chk("直接给出文件名", bool(d.get("file")), d.get("file"))
        job = d.get("job")
        c2, j2 = jget("/api/cover/job?id=" + str(job))
        chk("任务一创建就是 done", j2.get("job", {}).get("state") == "done",
            j2.get("job", {}).get("stage"))
        chk("成品库没有多出文件", mp3_set() == before,
            "%d -> %d" % (len(before), len(mp3_set())))

        print("\n== 3. /api/cover/exists（前端提示用） ==")
        c3, d3 = jget("/api/cover/exists?song_id=" + sid + "&model=" + model
                      + "&acapella=" + ("1" if ac else "0"))
        chk("exists 返回变体清单", c3 == 200 and d3.get("ok") and d3.get("variants"), c3)
        chk("exact=True（正好是这个组合）", d3.get("exact") is True)
        c4, d4 = jget("/api/cover/exists?song_id=" + sid + "&model=__none__.pth&acapella=0")
        chk("换声线时 exact=False", d4.get("exact") is False)
        c5, d5 = jget("/api/cover/exists?song_id=1")
        chk("没做过的歌 → 空清单", d5.get("ok") and not d5.get("variants"))
        c5b, d5b = jget("/api/guest/exists?song_id=" + sid + "&model=" + model
                        + "&acapella=" + ("1" if ac else "0"))
        chk("访客端 exists 也能命中（页面提示用）",
            c5b == 200 and d5b.get("exact") is True and d5b.get("labels"), d5b.get("labels"))

        print("\n== 4. 同参数并发：两次提交只跑一次 ==")
        calls.clear()
        body = {"song_id": "999999001", "title": "复用测试曲", "artist": "测试",
                "model": model, "key_shift": 3, "acapella": False}
        c6, a = jget("/api/cover/start", method="POST", body=body)
        c7, b = jget("/api/cover/start", method="POST", body=body)
        chk("第一次正常派发", c6 == 200 and a.get("ok") and not a.get("cached"), a.get("msg"))
        chk("第二次挂到同一个任务", b.get("job") == a.get("job") and b.get("shared") is True,
            b.get("msg"))
        chk("只起了一路子任务", len(calls) == 1, calls)
        # 等桩任务结束，确认台账被摘掉、下次能重新跑
        time.sleep(3.2)
        c8, e = jget("/api/cover/start", method="POST", body=body)
        chk("任务结束后台账已清，可重新派发", e.get("job") != a.get("job") and not e.get("shared"),
            e.get("msg"))

        print("\n== 5. 访客申请：命中成品 → 批准后立刻 done ==")
        c9, r = jget("/api/guest/request", method="POST",
                     body={"type": "cover", "song_id": sid, "title": ent.get("title"),
                           "artist": ent.get("artist"), "model": model, "acapella": ac})
        chk("提交成功且提示已有成品", c9 == 200 and r.get("ok") and r.get("reuse") is True,
            r.get("msg"))
        rid = (r.get("request") or {}).get("id")
        c10, ap = jget("/api/admin/approve", method="POST", body={"id": rid})
        chk("批准直接复用", ap.get("ok") and ap.get("reuse") is True, ap.get("msg"))
        c11, mine = jget("/api/admin/requests?state=all")
        row = next((x for x in (mine.get("requests") or []) if x["id"] == rid), None)
        chk("申请直接 done 并带上文件", bool(row) and row["state"] == "done" and row["file"],
            row and (row["state"], row["file"]))
        chk("申请上打了复用标记", bool(row) and row.get("reuse") is True)
        # 收尾：把这条测试申请清掉
        with guest._lock:
            guest._db["requests"] = [x for x in guest._db["requests"] if x["id"] != rid]
            guest._save()

        print("\n== 6. 朗读（TTS）同样内容复用 ==")
        tk = tts.text_key("复用测试文本", "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoxiaoNeural", "正常")
        chk("文字指纹稳定", tk == tts.text_key("复用测试文本", "zh-CN-XiaoxiaoNeural",
                                               "zh-CN-XiaoxiaoNeural", "正常"))
        chk("文字不同 → 指纹不同", tk != tts.text_key("复用测试文本2", "zh-CN-XiaoxiaoNeural",
                                                     "zh-CN-XiaoxiaoNeural", "正常"))
        chk("没做过的朗读 → 不命中", tts._find_cached(tk) is None)
        # 造一条归档记录，验证能被找到
        fake = os.path.join(cover.COVERS_DIR, "朗读-复用测试-晓晓.mp3")
        with open(fake, "wb") as f:
            f.write(b"\xff\xfb" + b"\x00" * 4096)
        items = tts.cover_index()
        items.append({"name": os.path.basename(fake), "title": "朗读：复用测试", "artist": "晓晓",
                      "model": "", "key_shift": None, "acapella": False, "song_id": "tts",
                      "created": time.time(), "tts_key": tk, "size": os.path.getsize(fake)})
        tts.save_index(items)
        got = tts._find_cached(tk)
        chk("同内容的朗读 → 命中归档", bool(got) and got["name"] == os.path.basename(fake))
        c12, ts = jget("/api/guest/request", method="POST",
                       body={"type": "tts", "text": "复用测试文本",
                             "voice": "zh-CN-XiaoxiaoNeural", "rate": "正常"})
        chk("访客朗读申请也提示已有成品", c12 == 200 and ts.get("ok") and ts.get("reuse") is True,
            ts.get("msg"))
        # 清掉伪造的朗读记录
        with guest._lock:
            guest._db["requests"] = [x for x in guest._db["requests"]
                                     if not (x.get("type") == "tts" and x.get("text") == "复用测试文本")]
            guest._save()
        tts.save_index([i for i in tts.cover_index() if i.get("name") != os.path.basename(fake)])
        try:
            os.remove(fake)
        except OSError:
            pass
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
