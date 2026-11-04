# -*- coding: utf-8 -*-
"""稳定性回归：把「已修过的数据/并发类 bug」逐条钉住。

这些问题共同点是**不会立刻报错**——空库被写回、索引被误删、缓存没生效、
日志轮转失效——所以必须靠断言而不是靠肉眼看。

    apienv\\Scripts\\python.exe e2e_stability.py

全部在临时目录里跑，不碰真实访客库/成品库。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="rvc_stab_")
os.environ["RVC_GUEST_DB"] = os.path.join(TMP, "guest_db.json")
os.environ["RVC_NO_AUTOPUMP"] = "1"

PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def main():
    import guest
    import cover
    import dataio
    import models_mgr
    import settings
    import perf
    import captcha
    import updater
    from panelkit import pages, security

    # ---------------- 访客库：读得动就读，读不动先留证据 ----------------
    print("\n[1] 访客库加载：BOM / 损坏 / 坏记录")
    dbp = guest.DB_PATH
    demo = {"visits": [{"id": "a" * 16, "last": time.time(), "count": 2}],
            "requests": [{"id": "r1", "gid": "a" * 16, "state": "pending",
                          "created": time.time()}],
            "queue": ["r1"], "wl_apps": [], "whitelist": []}
    with open(dbp, "w", encoding="utf-8-sig") as f:
        json.dump(demo, f)
    guest._load()
    chk("UTF-8 BOM 的库能正常读入（不再当空库）",
        len(guest._db["visits"]) == 1 and len(guest._db["requests"]) == 1,
        guest._db["visits"])

    with open(dbp, "w", encoding="utf-8") as f:
        f.write('[{"id": "a", ')          # 半截 JSON
    guest._load()
    bads = [n for n in os.listdir(TMP) if ".bad-" in n]
    chk("损坏的库被另存为 .bad-时间戳", bool(bads), bads)
    chk("坏库会在状态里给出原因（不是静默清空）", "读取失败" in guest.save_error(),
        guest.save_error())
    guest.replace_db(demo)                 # 还能继续用
    chk("坏库之后仍可正常写回", json.load(open(dbp, encoding="utf-8"))["requests"])

    print("\n[2] 坏条目不会让访客页 500")
    guest.replace_db({"visits": [{"id": "b" * 16, "last": "昨天", "count": None},
                                 "oops", {"no_id": 1}],
                      "requests": [], "queue": [], "wl_apps": [], "whitelist": []})
    try:
        v = guest.touch("b" * 16, "1.2.3.4", "", "UA")
        ok = isinstance(v, dict) and v.get("id") == "b" * 16
        err = ""
    except Exception as e:
        ok, err = False, "%s: %s" % (type(e).__name__, e)
    chk("last/count 是脏数据也不抛异常", ok, err)
    chk("_shape 剔掉字符串/缺 id 的条目",
        all(isinstance(x, dict) and x.get("id") for x in guest.snapshot()["visits"]))

    # ---------------- 模型：索引不能被子串误伤 ----------------
    print("\n[3] 删声线时不误删别人的索引")
    models_mgr._collect_indices = lambda: [
        ("added_IVF966_Flat_nprobe_1_kaito_v2.index", "x"),
        ("added_IVF12_Flat_nprobe_1_main_v2.index", "y"),
    ]
    chk("1.pth 不再命中全部索引", models_mgr._match_indices("1.pth") == [],
        models_mgr._match_indices("1.pth"))
    chk("ai.pth 不再命中 kaito/main", models_mgr._match_indices("ai.pth") == [])
    chk("kaito.pth 仍能精确命中自己的索引",
        models_mgr._match_indices("kaito.pth") ==
        ["added_IVF966_Flat_nprobe_1_kaito_v2.index"])

    # ---------------- 静态资源：盘符不能逃出目录 ----------------
    print("\n[4] /assets 不能借盘符读别处")
    chk("C:/x.js 被拒", pages.asset_abs("C:/x.js") is None)
    chk("../x.js 被拒", pages.asset_abs("../x.js") is None)
    chk("正常资源仍可取", pages.asset_abs("console.css") is not None)

    # ---------------- 设置缓存真的生效 ----------------
    print("\n[5] 高级设置缓存")
    settings._cache = None
    settings.get()
    chk("get() 之后模块级缓存被填上", settings._cache is not None)

    # ---------------- 硬件档位：整体替换，读者看不到空档 ----------------
    print("\n[6] perf 缓存竞态")
    a = perf.probe()
    chk("probe 带 tier/prewarm", "tier" in a and "prewarm" in a)
    b = perf.probe(force=True)
    chk("force 之后返回的是完整的新 dict", "tier" in b and "prewarm" in b)

    # ---------------- 验证码：超过最高档仍会锁 ----------------
    print("\n[7] 验证码阶梯锁定")
    ip = "9.9.9.9"
    for _ in range(captcha.LOCK_STEPS[-1] + 3):
        captcha._fail(ip)
    captcha._fails[ip]["until"] = 0        # 模拟上一轮锁已过期
    captcha._fail(ip)
    chk("连错 13 次以上、冷却结束后再错仍会锁",
        captcha._wait_secs(ip) > 0, captcha._fails[ip])

    # ---------------- 成品索引：并发追加不丢 ----------------
    print("\n[8] 成品索引并发写")
    cover.INDEX_PATH = os.path.join(TMP, "covers_index.json")
    cover.COVERS_DIR = os.path.join(TMP, "covers")
    os.makedirs(cover.COVERS_DIR, exist_ok=True)
    with open(cover.INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump([], f)

    def add_many(tag):
        for i in range(20):
            cover._mutate_index(lambda items: items.append({"name": "%s-%d.mp3" % (tag, i)}))

    ths = [threading.Thread(target=add_many, args=("t%d" % k,)) for k in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    items = cover._load_index()
    chk("6 线程 × 20 条全部保留（读-改-写同一把锁）", len(items) == 120, len(items))

    print("\n[9] 并发归档抢名")
    names = []
    lk = threading.Lock()

    made = []

    def claim():
        n, p = cover._claim_name("同名-原调-x")
        with lk:
            names.append(n)
            made.append(p)          # 文件留着，最后一起删（删了就能被重新抢到）

    ths = [threading.Thread(target=claim) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    chk("8 个并发请求拿到 8 个不同文件名", len(set(names)) == 8, names)
    for p in made:
        try:
            os.remove(p)
        except OSError:
            pass

    print("\n[10] 索引文件损坏时先留证据")
    with open(cover.INDEX_PATH, "w", encoding="utf-8") as f:
        f.write('[{"name": "a.mp3"},')
    got = cover._load_index()
    bads = [n for n in os.listdir(TMP) if "covers_index.json.bad-" in n]
    chk("坏索引返回空表但留下 .bad 备份", got == [] and bool(bads), bads)
    chk("索引错误能被状态接口看到", bool(cover.index_error()))

    # ---------------- 导入合并：已完成的单不被降级 ----------------
    print("\n[11] 合并导入不把 done 降成 error")
    cur = {"requests": [{"id": "x", "state": "done", "file": "a.mp3",
                         "decided": 100, "created": 10}],
           "visits": [], "wl_apps": [], "whitelist": [], "queue": []}
    inc = {"requests": [{"id": "x", "state": "error", "decided": 200, "created": 10}],
           "visits": [], "wl_apps": [], "whitelist": [], "queue": []}
    merged, _ = dataio._merge_guest_db(cur, inc)
    r = merged["requests"][0]
    chk("done 保持 done 且成品链接还在", r["state"] == "done" and r["file"] == "a.mp3", r)

    print("\n[12] 导入时不把本机在跑的 job 打回队列")
    d = {"requests": [{"id": "y", "state": "running", "job": ""}],
         "visits": [], "wl_apps": [], "whitelist": [], "queue": []}
    out, _ = dataio._normalize(d)
    chk("没有 job 句柄的 running 改回 queued", out["requests"][0]["state"] == "queued")

    # ---------------- 版本比较 ----------------
    print("\n[13] 版本号比较")
    chk("2.0.1.5 > 2.0.1", updater.is_newer("2.0.1.5", "2.0.1"))
    chk("2.1.0 > 2.0.9", updater.is_newer("2.1.0", "2.0.9"))
    chk("相等不提示更新", not updater.is_newer("2.0.1", "2.0.1"))

    # ---------------- 安全日志轮转 ----------------
    print("\n[14] 安全日志超过 512KB 仍能继续写")
    logp = os.path.join(TMP, "security.log")
    with open(logp, "wb") as f:
        f.write(b"x" * (600 * 1024) + b"\n")
    security.SEC_LOG = logp
    security.sec_log("轮转之后的测试行")
    size = os.path.getsize(logp)
    tail = open(logp, "rb").read()[-200:]
    chk("文件被截短（轮转真的执行了）", size < 600 * 1024, size)
    chk("新日志仍然写得进去", "轮转之后的测试行".encode() in tail)

    print("\n" + "=" * 52)
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  - " + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
