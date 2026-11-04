# -*- coding: utf-8 -*-
"""数据备份回归：导出 → 校验 → 覆盖 / 合并导入 → 失败回滚。

全部在临时目录里造数据（不碰真实 guest_db.json 与成品库），
用 8976 起一个临时实例跑接口。
    python e2e_dataio.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="rvc_dio_")
DB_P = os.path.join(TMP, "guest_db.json")
SET_P = os.path.join(TMP, "advanced_settings.json")
COV_P = os.path.join(TMP, "covers")
BAK_P = os.path.join(TMP, "_backup")
IDX_P = os.path.join(COV_P, "index.json")

# 必须在 import panel（进而 import dataio）之前设好：BACKUP_DIR 是模块级常量
os.environ["RVC_PANEL_PORT"] = "8976"
os.environ["RVC_NO_AUTOPUMP"] = "1"
os.environ["RVC_NO_BROWSER"] = "1"
os.environ["RVC_GUEST_DB"] = DB_P
os.environ["RVC_SETTINGS_PATH"] = SET_P
os.environ["RVC_COVERS_DIR"] = COV_P
os.environ["RVC_BACKUP_DIR"] = BAK_P

import panel      # noqa: E402
import dataio     # noqa: E402
import guest      # noqa: E402
import settings as settings_mod   # noqa: E402

BASE = "http://127.0.0.1:8976"
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jpost(path, body=None):
    data = json.dumps(body or {}).encode()
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"raw": raw}
    except OSError as e:
        return 0, {"msg": str(e)}


def jget(path, raw=False):
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as resp:
            body = resp.read()
            if raw:
                return resp.status, body, dict(resp.headers)
            return resp.status, json.loads(body.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        body = e.read()
        if raw:
            return e.code, body, dict(e.headers or {})
        try:
            return e.code, json.loads(body.decode("utf-8", "ignore") or "{}")
        except Exception:
            return e.code, {}
    except OSError:
        return 0, (b"" if raw else {})


# ---------------- 造数据 ----------------
def base_db():
    now = time.time()
    return {
        "visits": [{"id": "g%d" % i, "ip": "1.1.1.%d" % i, "country": "CN",
                    "ua": "x", "raw_ua": "x", "first": now - 500, "last": now - i,
                    "count": i + 1} for i in range(5)],
        "requests": [
            {"id": "r1", "gid": "g0", "state": "done", "created": now - 100,
             "decided": now - 90, "title": "歌一", "file": "a.mp3", "size_text": "1 MB"},
            {"id": "r2", "gid": "g1", "state": "running", "created": now - 60,
             "job": "job-xyz", "file": "half.mp3", "title": "歌二"},
            {"id": "r3", "gid": "g2", "state": "pending", "created": now - 20,
             "title": "歌三"},
        ],
        "queue": ["r2"],
        "wl_apps": [{"id": "w1", "gid": "g0", "state": "pending", "created": now}],
        "whitelist": [{"gid": "g0", "fp": "f" * 32, "ip": "1.1.1.0",
                       "approved": True, "hits": 3, "last_seen": now}],
    }


def seed(db=None, idx=None, settings=None):
    os.makedirs(COV_P, exist_ok=True)
    json.dump(db if db is not None else base_db(),
              open(DB_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(settings if settings is not None else {"max_parallel": 2, "f0_method": "rmvpe"},
              open(SET_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if idx is None:
        idx = [{"name": "a.mp3", "title": "歌一", "artist": "A", "model": "m.pth",
                "song_id": "1", "created": time.time() - 100, "size": 100}]
    json.dump(idx, open(IDX_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    guest._load()
    settings_mod.load()


def disk_db():
    try:
        return json.load(open(DB_P, encoding="utf-8"))
    except Exception:
        return {}


def disk_idx():
    try:
        return json.load(open(IDX_P, encoding="utf-8"))
    except Exception:
        return []


def clear_backups():
    if os.path.isdir(BAK_P):
        shutil.rmtree(BAK_P, ignore_errors=True)


def craft(zip_path, manifest, entries):
    """手搓一个包，用来测各种坏包。"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in entries.items():
            zf.writestr(arc, data)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False).encode())
    return zip_path


def main():
    seed()
    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8976), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)
    try:
        print("\n== 1. 概况 ==")
        s, sm = jget("/api/data/summary")
        chk("接口可用", s == 200 and sm.get("ok"), sm.get("msg"))
        chk("数得清访客与申请",
            sm["counts"]["visits"] == 5 and sm["counts"]["requests"] == 3,
            sm["counts"])
        chk("统计出各状态条数",
            sm["counts"]["running"] == 1 and sm["counts"]["done"] == 1
            and sm["counts"]["pending"] == 1)
        chk("成品索引也数了", sm["covers"]["indexed"] == 1, sm["covers"])
        chk("备份目录一开始是空的", sm["backups"] == [], sm["backups"])

        print("\n== 2. 导出 ==")
        s, ex = jpost("/api/data/export", {"audio": False})
        chk("导出成功", s == 200 and ex.get("ok"), ex.get("msg"))
        zp = ex.get("path")
        chk("四类数据都进包了",
            sorted(i["arc"] for i in ex["items"]) ==
            ["data/advanced_settings.json", "data/covers_index.json",
             "data/guest_db.json", "models/manifest.json"],
            [i["arc"] for i in ex["items"]])
        chk("包落在备份目录里", os.path.isfile(zp) and zp.startswith(BAK_P), zp)
        with zipfile.ZipFile(zp) as zf:
            man = json.loads(zf.read("manifest.json").decode())
            chk("manifest 标了来源与格式",
                man["app"] == dataio.APP and man["kind"] == dataio.KIND
                and man["schema"] == dataio.SCHEMA, (man["app"], man["kind"], man["schema"]))
            chk("manifest 带得上库里的条数", man["counts"]["visits"] == 5, man["counts"])
            ok_sha = all(hashlib.sha256(zf.read(f["arc"])).hexdigest() == f["sha256"]
                         for f in man["files"] if f.get("sha256"))
            chk("每个条目都有正确的 sha256", ok_sha)
            chk("包里有声线清单", "models/manifest.json" in zf.namelist())
        s, body, hdr = jget("/api/data/file?name=" + urllib.parse.quote(ex["name"]), raw=True)
        chk("下载接口返回 zip", s == 200 and body[:2] == b"PK", s)
        cd = hdr.get("Content-Disposition") or hdr.get("content-disposition") or ""
        chk("下载头带 UTF-8 文件名（中文不乱码）",
            "attachment" in cd and "filename*=UTF-8''" in cd, cd[:90])
        chk("下载到的就是刚打包的那份",
            hashlib.sha256(body).hexdigest() == hashlib.sha256(open(zp, "rb").read()).hexdigest())

        print("\n== 3. 预检（只读，不改数据）==")
        before = disk_db()
        s, ck = jpost("/api/data/import_check", {"path": zp})
        chk("正常包通过预检", s == 200 and ck.get("ok"), ck.get("msg"))
        chk("给出 token", bool(ck.get("token")), (ck.get("token") or "")[:12])
        chk("预检不改动本机库", disk_db() == before)
        chk("包里有什么 / 本机有什么 都列了",
            ck["counts"]["visits"] == 5 and ck["current"]["visits"] == 5)
        chk("覆盖导入后：在跑的单会变排队",
            ck["preview"]["replace"]["queued"] == 1
            and ck["preview"]["replace"]["running"] == 0, ck["preview"]["replace"])
        chk("给了合并后的预估", isinstance(ck["preview"].get("merge"), dict),
            ck["preview"].get("merge"))

        print("\n== 4. 坏包一律拒收 ==")
        bad = os.path.join(TMP, "bad.zip")
        open(bad, "wb").write(b"this is not a zip")
        chk("不是 zip", jpost("/api/data/import_check", {"path": bad})[1].get("ok") is False)
        chk("不存在的文件",
            jpost("/api/data/import_check", {"path": os.path.join(TMP, "nope.zip")})[1].get("ok") is False)
        chk("不认识的扩展名",
            jpost("/api/data/import_check", {"path": DB_P})[1].get("ok") is False)

        no_man = os.path.join(TMP, "no_man.zip")
        with zipfile.ZipFile(no_man, "w") as zf:
            zf.writestr("data/guest_db.json", b"{}")
        r = jpost("/api/data/import_check", {"path": no_man})[1]
        chk("缺 manifest 拒收", not r.get("ok") and "manifest" in (r.get("msg") or ""), r.get("msg"))

        def mk(name, man, entries):
            man.setdefault("app", dataio.APP)
            man.setdefault("kind", dataio.KIND)
            man.setdefault("schema", dataio.SCHEMA)
            return craft(os.path.join(TMP, name), man, entries)

        r = jpost("/api/data/import_check",
                  {"path": mk("wrongapp.zip", {"app": "other-app", "files": []}, {})})[1]
        chk("别的程序的包拒收", not r.get("ok"), r.get("msg"))
        r = jpost("/api/data/import_check",
                  {"path": mk("future.zip", {"schema": 99, "files": []}, {})})[1]
        chk("更高版本的包提示先升级", not r.get("ok") and "升级" in (r.get("msg") or ""),
            r.get("msg"))

        payload = b'{"visits": [], "requests": []}'
        r = jpost("/api/data/import_check", {"path": mk("tamper.zip", {"files": [
            {"arc": "data/guest_db.json", "bytes": len(payload),
             "sha256": "0" * 64}]}, {"data/guest_db.json": payload})})[1]
        chk("内容被改过（sha 不符）拒收", not r.get("ok") and "改动" in (r.get("msg") or ""),
            r.get("msg"))

        r = jpost("/api/data/import_check", {"path": mk("trunc.zip", {"files": [
            {"arc": "data/guest_db.json", "bytes": 99999,
             "sha256": hashlib.sha256(payload).hexdigest()}]},
            {"data/guest_db.json": payload})})[1]
        chk("大小对不上拒收", not r.get("ok") and "大小" in (r.get("msg") or ""), r.get("msg"))

        r = jpost("/api/data/import_check", {"path": mk("missing.zip", {"files": [
            {"arc": "data/guest_db.json", "bytes": 1, "sha256": ""}]}, {})})[1]
        chk("清单里的文件不在包里", not r.get("ok"), r.get("msg"))

        r = jpost("/api/data/import_check", {"path": mk("slip.zip", {"files": [
            {"arc": "data/../../evil.json", "bytes": 2, "sha256": ""}]},
            {"data/../../evil.json": b"{}"})})[1]
        chk("路径穿越（../）拒收", not r.get("ok") and "路径" in (r.get("msg") or ""),
            r.get("msg"))
        r = jpost("/api/data/import_check", {"path": mk("slip2.zip", {"files": [
            {"arc": "covers/../../x.mp3", "bytes": 2, "sha256": ""}]},
            {"covers/../../x.mp3": b"xx"})})[1]
        chk("成品路径也守得住", not r.get("ok"), r.get("msg"))

        r = jpost("/api/data/import_check", {"path": mk("empty.zip", {"files": []}, {})})[1]
        chk("空清单拒收", not r.get("ok"), r.get("msg"))

        print("\n== 5. 覆盖导入 ==")
        before_db = disk_db()
        r = jpost("/api/data/import_apply", {"path": zp, "token": ck["token"], "mode": "replace"})[1]
        chk("没勾确认不让导（防误点）",
            not r.get("ok") and "确认" in (r.get("msg") or ""), r.get("msg"))
        chk("拒绝时数据一个字节没动", disk_db() == before_db)

        r = jpost("/api/data/import_apply",
                  {"path": zp, "token": "deadbeef" * 8, "mode": "replace", "confirm": True})[1]
        chk("token 不符拒绝", not r.get("ok") and "改动" in (r.get("msg") or ""), r.get("msg"))

        r = jpost("/api/data/import_apply",
                  {"path": zp, "token": ck["token"], "mode": "replace", "confirm": True})[1]
        chk("覆盖导入成功", r.get("ok"), r.get("msg"))
        d = disk_db()
        chk("访客数一致", len(d["visits"]) == 5, len(d["visits"]))
        chk("申请数一致", len(d["requests"]) == 3, len(d["requests"]))
        got = {x["id"]: x for x in d["requests"]}
        chk("原来在跑的那单改回排队", got["r2"]["state"] == "queued", got["r2"]["state"])
        chk("跑了一半的 job/file 引用被清掉",
            "job" not in got["r2"] and "file" not in got["r2"], got["r2"])
        chk("队列按原顺序重建", d["queue"] == ["r2"], d["queue"])
        chk("内存缓存同步刷新（不用重启面板）",
            len(guest._db["requests"]) == 3 and guest._db["queue"] == ["r2"],
            (len(guest._db["requests"]), guest._db["queue"]))
        chk("导入前自动存了一份", bool(r.get("backup")) and os.path.isfile(r["backup"]),
            r.get("backup_name"))
        chk("自动存档名字能看出用途", "导入前自动备份" in (r.get("backup_name") or ""),
            r.get("backup_name"))

        print("\n== 6. 合并导入 ==")
        cur = disk_db()
        inc = base_db()
        inc["visits"].append({"id": "g9", "ip": "9.9.9.9", "country": "JP", "ua": "y",
                              "raw_ua": "y", "first": time.time(), "last": time.time(),
                              "count": 1})
        inc["requests"].append({"id": "r9", "gid": "g9", "state": "done",
                                "created": time.time(), "title": "新歌"})
        inc["whitelist"].append({"gid": "g8", "fp": "a" * 32, "ip": "8.8.8.8",
                                 "approved": True, "hits": 1, "last_seen": time.time()})
        # 同一条 r3 在备份里已被拒绝（更新的决定）→ 合并应取这个状态
        for x in inc["requests"]:
            if x["id"] == "r3":
                x["state"] = "rejected"
                x["decided"] = time.time() + 5
        # 正规途径：把 inc 写进库导出一份，再把库还原成 cur，然后合并导入
        json.dump(inc, open(DB_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        guest._load()
        z3 = dataio.export()
        json.dump(cur, open(DB_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        guest._load()
        ck3 = dataio.check(z3["path"])
        r = jpost("/api/data/import_apply",
                  {"path": z3["path"], "token": ck3["token"], "mode": "merge", "confirm": True})[1]
        chk("合并导入成功", r.get("ok"), r.get("msg"))
        d = disk_db()
        n_visit = len(d["visits"])
        chk("新访客补进来了", n_visit == 6, n_visit)
        chk("原有访客没被复制成两条", len({x["id"] for x in d["visits"]}) == n_visit)
        ids = [x["id"] for x in d["requests"]]
        chk("原有申请没被复制", len(ids) == len(set(ids)), ids)
        chk("新申请进来了", "r9" in ids, ids)
        r3 = [x for x in d["requests"] if x["id"] == "r3"][0]
        chk("同一条取更新的那次状态", r3["state"] == "rejected", r3["state"])
        chk("白名单并集", len(d["whitelist"]) == 2, d["whitelist"])
        chk("合并统计报得出来", (r.get("stats") or {}).get("visits_added") == 1,
            r.get("stats"))

        print("\n== 7. 同一个包导两次不会翻倍 ==")
        ck4 = dataio.check(z3["path"])
        before_n = len(disk_db()["requests"])
        r = jpost("/api/data/import_apply",
                  {"path": z3["path"], "token": ck4["token"], "mode": "merge", "confirm": True})[1]
        chk("第二次合并成功", r.get("ok"), r.get("msg"))
        chk("条数没变", len(disk_db()["requests"]) == before_n,
            (before_n, len(disk_db()["requests"])))
        chk("这次没新增", (r.get("stats") or {}).get("requests_added") == 0, r.get("stats"))

        print("\n== 8. 高级设置与成品索引也一起回来 ==")
        seed(settings={"max_parallel": 3, "f0_method": "crepe"}, idx=[
            {"name": "s1.mp3", "title": "设置测试", "created": 1.0, "song_id": "9"}])
        z4 = dataio.export()
        seed(settings={"max_parallel": 1, "f0_method": "rmvpe"}, idx=[])
        ck5 = dataio.check(z4["path"])
        r = jpost("/api/data/import_apply",
                  {"path": z4["path"], "token": ck5["token"], "mode": "replace",
                   "confirm": True})[1]
        sg = settings_mod.get()
        chk("设置导入后立刻生效",
            sg["max_parallel"] == 3 and sg["f0_method"] == "crepe", sg)
        chk("索引也回来了", len(disk_idx()) == 1, disk_idx())
        chk("设置越界值会被夹住而不是写坏",
            dataio._settings().save({"max_parallel": 999}).get("ok") is True
            and settings_mod.get()["max_parallel"] == 6, settings_mod.get())

        print("\n== 9. 连成品音频一起打包 ==")
        open(os.path.join(COV_P, "s1.mp3"), "wb").write(b"ID3" + b"\x00" * 5000)
        z5 = dataio.export(with_audio=True)
        chk("音频进包", z5.get("audio_files") == 1, z5.get("audio_files"))
        with zipfile.ZipFile(z5["path"]) as zf:
            chk("在 covers/ 下", "covers/s1.mp3" in zf.namelist(), zf.namelist())
        os.remove(os.path.join(COV_P, "s1.mp3"))
        ck6 = dataio.check(z5["path"])
        r = jpost("/api/data/import_apply",
                  {"path": z5["path"], "token": ck6["token"], "mode": "replace",
                   "confirm": True})[1]
        chk("导入把音频放回成品库",
            os.path.isfile(os.path.join(COV_P, "s1.mp3")) and r.get("ok"),
            (r.get("msg"), r.get("stats")))

        print("\n== 10. 失败要整体回滚 ==")
        seed(db=base_db())
        good = disk_db()
        real_write = dataio._write_atomic
        cnt = {"n": 0}

        def boom(path, data):
            # 按目标文件注入，而不是「第 N 次写」：导入顺序会随实现变化，
            # 写死次数的话改一次实现就得跟着改测试，还容易测错东西。
            if not cnt["n"] and os.path.realpath(path) == os.path.realpath(IDX_P):
                cnt["n"] += 1
                raise OSError("磁盘满了（测试桩）")
            return real_write(path, data)

        dataio._write_atomic = boom
        try:
            ck7 = dataio.check(z5["path"])
            r = dataio.apply(z5["path"], token=ck7["token"], mode="replace")
        finally:
            dataio._write_atomic = real_write
        chk("确实炸到了成品索引那一步（否则这条没测到东西）", cnt["n"] == 1, cnt["n"])
        chk("失败被捕获并回滚", (not r.get("ok")) and r.get("rolled_back"), r.get("msg"))
        chk("库回到导入前（一条不差）", disk_db() == good, len(disk_db().get("requests") or []))
        chk("内存也回到导入前（不是只还原了文件）",
            [x.get("id") for x in (guest._db.get("requests") or [])]
            == [x.get("id") for x in (good.get("requests") or [])],
            [x.get("id") for x in (guest._db.get("requests") or [])])

        print("\n== 11. 容量上限 ==")
        big = base_db()
        big["visits"] = [{"id": "v%d" % i, "ip": "2.2.2.%d" % (i % 250), "country": "CN",
                          "ua": "z", "raw_ua": "z", "first": i, "last": i, "count": 1}
                         for i in range(600)]
        big["requests"] = ([{"id": "p%d" % i, "gid": "g0", "state": "pending",
                             "created": i, "title": "x"} for i in range(320)])
        seed(db=big)
        z6 = dataio.export()
        seed(db=base_db())
        ck8 = dataio.check(z6["path"])
        r = jpost("/api/data/import_apply",
                  {"path": z6["path"], "token": ck8["token"], "mode": "replace",
                   "confirm": True})[1]
        d = disk_db()
        chk("访客截到上限", len(d["visits"]) == guest.MAX_VISITS, len(d["visits"]))
        chk("申请截到上限", len(d["requests"]) == guest.MAX_REQ, len(d["requests"]))
        chk("待批超过上限时只留最新的 300 条",
            sum(1 for x in d["requests"] if x["state"] == "pending") == guest.MAX_REQ,
            len(d["requests"]))

        print("\n== 12. 备份管理 ==")
        clear_backups()
        for _ in range(dataio.MAX_KEEP_AUTO + 3):
            seed()
            dataio.export(kind="auto")
        bks = dataio.list_backups(limit=100)
        chk("自动备份按份数裁剪（不会撑爆盘）",
            len([x for x in bks if x["auto"]]) <= dataio.MAX_KEEP_AUTO,
            len([x for x in bks if x["auto"]]))
        nm = bks[0]["name"]
        r = jpost("/api/data/delete", {"name": nm})[1]
        chk("能删掉某一份", r.get("ok") and not os.path.isfile(os.path.join(BAK_P, nm)),
            r.get("msg"))
        r = jpost("/api/data/delete", {"name": "../guest_db.json"})[1]
        chk("删不到备份目录以外的文件", not r.get("ok"), r.get("msg"))
        chk("本机库还在", os.path.isfile(DB_P))
        r = jpost("/api/data/delete", {"name": "不存在.zip"})[1]
        chk("删不存在的给出提示", not r.get("ok"), r.get("msg"))

        print("\n== 13. 空库 / 全新机器 ==")
        clear_backups()
        for p in (DB_P, SET_P, IDX_P):
            if os.path.isfile(p):
                os.remove(p)
        guest._db = {"visits": [], "requests": [], "queue": [], "wl_apps": [], "whitelist": []}
        guest._dirty = False      # 别让 flush 反过来又写出一份空库
        e = dataio.export()
        chk("没数据时导出给出人话", (not e.get("ok")) and e.get("empty"), e.get("msg"))
        seed()
        z7 = dataio.export()
        for p in (DB_P, SET_P):
            if os.path.isfile(p):
                os.remove(p)
        ck9 = dataio.check(z7["path"])
        r = jpost("/api/data/import_apply",
                  {"path": z7["path"], "token": ck9["token"], "mode": "replace",
                   "confirm": True})[1]
        chk("全新机器也能导入（没有导入前备份不算失败）", r.get("ok"), r.get("msg"))
        chk("数据真的落回来了", len(disk_db()["requests"]) == 3)

        print("\n== 14. 导入时有人来访，也不会把导入结果冲掉 ==")
        # 曾经的坑：apply 先写文件、最后才 reload 内存；这中间巡队线程的 flush()
        # 或访客的一次 _save() 会把**旧内存**写回去，导入白做、接口还返回成功。
        clear_backups()
        seed()                                   # 包的内容：3 单（r1/r2/r3）
        z8 = dataio.export()
        guest.replace_db({"visits": [], "queue": [], "wl_apps": [], "whitelist": [],
                          "requests": [{"id": "old1", "gid": "g0", "state": "done",
                                        "created": 1, "title": "旧的"}]})
        # 制造「有未落盘改动」：访客刚来过，soft 写被节流
        guest._db["visits"].append({"id": "v-new", "ip": "9.9.9.9", "country": "CN",
                                    "ua": "x", "raw_ua": "x", "first": time.time(),
                                    "last": time.time(), "count": 1})
        guest._last_save = time.time()
        guest._save(soft=True)
        chk("先确认脏标记真的在", guest._dirty is True)

        real_settings = dataio._settings()

        class SlowSettings:
            """让「写库之后、收尾之前」有一段真实窗口（线上这段是解压音频）。"""

            def __getattr__(self, name):
                return getattr(real_settings, name)

            def save(self, vals):
                time.sleep(0.35)
                return real_settings.save(vals)

        stop = threading.Event()

        def hammer():                            # 模拟巡队 flush + 访客来访的写盘
            while not stop.is_set():
                guest.flush()
                guest._save(soft=True)
                time.sleep(0.01)

        dataio._settings = lambda: SlowSettings()
        th = threading.Thread(target=hammer, daemon=True)
        th.start()
        try:
            ck10 = dataio.check(z8["path"])
            r = jpost("/api/data/import_apply",
                      {"path": z8["path"], "token": ck10["token"],
                       "mode": "replace", "confirm": True})[1]
        finally:
            stop.set()
            th.join(2.0)
            dataio._settings = lambda: real_settings

        chk("并发写盘时导入仍然成功", r.get("ok"), r.get("msg"))
        chk("磁盘上就是包里的 3 单（没被旧内存盖回去）",
            len(disk_db().get("requests") or []) == 3,
            [x.get("id") for x in (disk_db().get("requests") or [])])
        chk("内存也换成了导入后的内容",
            len(guest._db.get("requests") or []) == 3,
            len(guest._db.get("requests") or []))
        guest.flush()                            # 事后再让巡队写一次
        chk("事后再写盘也不会退回旧数据",
            len(disk_db().get("requests") or []) == 3,
            [x.get("id") for x in (disk_db().get("requests") or [])])

        print("\n== 15. 页面接线 ==")
        cpage = (open(os.path.join(HERE, "console_settings_page.html"), encoding="utf-8").read()
                 + open(os.path.join(HERE, "assets", "page_settings.js"), encoding="utf-8").read())
        chk("控制台有这张卡片", "数据备份与迁移" in cpage and 'id="dioSum"' in cpage)
        chk("三个动作都在", all(k in cpage for k in
                                ("dioExport()", "dioPick()", "dioOpen()")))
        chk("预检会展示三种数字", "覆盖导入后" in cpage and "合并导入后" in cpage)
        chk("导入前要求勾确认", 'id="dioSure"' in cpage and "我确认要改写本机数据" in cpage)
        chk("导完告诉用户自动存档在哪", "导入前的本机数据已存档" in cpage)
        chk("导出可选带音频", 'id="dioAudio"' in cpage)
        chk("备份列表能下载与删除", "dioGet(" in cpage and "dioDel(" in cpage)
        chk("接口路径对得上",
            all(p in cpage for p in ("/api/data/summary", "/api/data/export",
                                     "/api/data/import_check", "/api/data/import_apply")))
    finally:
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
        print("\n=== %d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项：" + "；".join(FAIL))


if __name__ == "__main__":
    main()
