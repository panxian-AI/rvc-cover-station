# -*- coding: utf-8 -*-
"""模型管理回归测试：列表 / index 配对 / 孤儿索引 / 两段式删除 / 路径穿越拦截，
外加 UVR 分离模型「按需加载 + 释放」的桩测试（不加载真模型，不吃内存）。

用 8897 起一个临时面板实例（不碰 8899），目录全部指到临时文件夹：
    apienv\\Scripts\\python.exe e2e_models.py
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
os.environ["RVC_PANEL_PORT"] = "8897"
os.environ["RVC_NO_AUTOPUMP"] = "1"

import panel   # noqa: E402
import models_mgr as mm   # noqa: E402

BASE = "http://127.0.0.1:8897"
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jpost(path, body=None):
    data = json.dumps(body or {}).encode()
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def jget(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


# ---------------- UVR 懒加载桩测试 ----------------

def test_lazy_uvr():
    print("\n== A. UVR 分离模型按需加载（桩，不碰真模型） ==")
    src = open(os.path.join(HERE, "..", "RVCSVC-API", "app_rvc.py"),
               encoding="utf-8").read().replace("\r\n", "\n")
    a = src.index("func = AudioPre")
    b = src.index("from pydub import AudioSegment")
    ns = {
        "os": os, "time": time, "gc": __import__("gc"),
        "AudioPre": None, "func": None, "device": "cpu", "is_half": False,
        "weight_uvr5_root": tempfile.mkdtemp(prefix="rvc_uvr_root_"),
        "torch": __import__("types").SimpleNamespace(
            cuda=__import__("types").SimpleNamespace(is_available=lambda: False)),
    }

    class FakePre:
        loads = []
        def __init__(self, agg=10, model_path="", device="cpu", is_half=False, tta=False):
            self.model_path = model_path
            self.data = {}
            self.agg = agg
            FakePre.loads.append(model_path)

    ns["AudioPre"] = FakePre
    ns["func"] = FakePre
    exec(src[a:b], ns)   # 把懒加载那一段原样跑起来

    chk("启动时未加载任何分离模型", len(FakePre.loads) == 0, FakePre.loads)
    f1 = ns["get_pre_fun"]()
    chk("首次 get_pre_fun 现场加载 1 次", len(FakePre.loads) == 1, FakePre.loads)
    chk("默认模型名正确", FakePre.loads[0].endswith("5_HP-Karaoke-UVR.pth"), FakePre.loads[0])
    f2 = ns["get_pre_fun"]()
    chk("第二次取走缓存，不重复加载", f1 is f2 and len(FakePre.loads) == 1)
    chk("取用时间被记录", time.time() - ns["_uvr5_last_used"][0] < 5)

    chk("release_pre_fun 释放成功", ns["release_pre_fun"]() is True)
    chk("再释放一次返回 False（本来就空）", ns["release_pre_fun"]() is False)
    ns["get_pre_fun"]()
    chk("释放后再取会重新加载", len(FakePre.loads) == 2, FakePre.loads)

    # ensure_pre_fun：坏模型名回退默认；好模型名切换
    default = ns["_uvr5"]["model"]
    ns["ensure_pre_fun"]("不存在的模型.pth")
    chk("不存在的分离模型沿用默认", ns["_uvr5"]["model"] == default, ns["_uvr5"]["model"])
    alt = os.path.join(ns["weight_uvr5_root"], "其它分离.pth")
    open(alt, "wb").write(b"x")
    ns["ensure_pre_fun"]("其它分离")          # 自动补 .pth
    ns["get_pre_fun"]()
    chk("有效模型名自动补 .pth 并切换", ns["_uvr5"]["model"] == "其它分离.pth",
        ns["_uvr5"]["model"])
    chk("切换后按新模型重新加载", len(FakePre.loads) == 3
        and FakePre.loads[-1].endswith("其它分离.pth"), FakePre.loads)
    chk("参数写到实例 data 上", ns["_uvr5"]["fun"].data.get("agg") == 10
        and ns["_uvr5"]["fun"].data.get("high_end_process") == "mirroring",
        ns["_uvr5"]["fun"].data)
    chk("默认空闲卸载 15 分钟", ns["UVR_IDLE_UNLOAD"] == 900.0, ns["UVR_IDLE_UNLOAD"])


# ---------------- 模型管理 HTTP 测试 ----------------

def main():
    test_lazy_uvr()

    print("\n== B. 模型管理（临时面板 8897） ==")
    tmp = tempfile.mkdtemp(prefix="rvc_mm_")
    wdir = os.path.join(tmp, "assets", "weights")
    ldir = os.path.join(tmp, "logs")
    os.makedirs(wdir)
    os.makedirs(ldir)
    mm.WEIGHTS_DIR = wdir
    mm.INDEX_DIRS = (ldir,)

    def mk(name, mb=2.5):
        p = os.path.join(wdir, name)
        with open(p, "wb") as f:
            f.write(b"\0" * int(mb * 1048576))
        return p

    def mkidx(name, mb=1.0):
        p = os.path.join(ldir, name)
        with open(p, "wb") as f:
            f.write(b"\0" * int(mb * 1048576))
        return p

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8897), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    # 空目录
    st, txt = jget("/api/models/list")
    d = json.loads(txt) if txt else {}
    chk("空目录列表 ok", st == 200 and d.get("ok") and d.get("count") == 0, (st, txt[:80]))

    mk("mygf-tf.pth")
    mk("soyo.pth")
    mkidx("added_IVF966_Flat_nprobe_1_mygf-tf_v2.index")
    mkidx("added_IVF64_Flat_nprobe_1_soyo_v2.index")
    mkidx("added_IVF9_Flat_nprobe_1_gone_v2.index")     # 孤儿：没有对应 pth

    st, txt = jget("/api/models/list")
    d = json.loads(txt)
    names = {m["name"]: m for m in d.get("models", [])}
    chk("列表认出 2 个模型", d.get("count") == 2, d.get("count"))
    chk("mygf-tf 配到 index", "added_IVF966_Flat_nprobe_1_mygf-tf_v2.index"
        in names.get("mygf-tf.pth", {}).get("indices", []), names.get("mygf-tf.pth"))
    chk("soyo 配到 index", any("soyo" in i for i in
        names.get("soyo.pth", {}).get("indices", [])))
    chk("孤儿索引单列", [o["name"] for o in d.get("orphans", [])]
        == ["added_IVF9_Flat_nprobe_1_gone_v2.index"], d.get("orphans"))
    chk("大小统计对", d.get("total_mb") == 5.0, d.get("total_mb"))

    st, page = jget("/models")
    chk("/models 页面可打开", st == 200 and "模型管理" in page, st)

    # 删除：先预览（不动文件），再确认
    st, d = jpost("/api/models/delete", {"name": "mygf-tf.pth"})
    chk("预览返回 2 个文件（pth+index）", d.get("preview") and d.get("count") == 2, d)
    chk("预览阶段文件还在", os.path.isfile(os.path.join(wdir, "mygf-tf.pth")))
    st, d = jpost("/api/models/delete", {"name": "mygf-tf.pth", "confirm": True})
    chk("确认删除 ok 且腾 3.5MB", d.get("ok") and d.get("mb") == 3.5, d)
    chk("pth 已删", not os.path.isfile(os.path.join(wdir, "mygf-tf.pth")))
    chk("配套 index 一起删了", not os.path.isfile(
        os.path.join(ldir, "added_IVF966_Flat_nprobe_1_mygf-tf_v2.index")))
    chk("别的模型没被误删", os.path.isfile(os.path.join(wdir, "soyo.pth")))

    # 路径穿越 / 非法名 / 重复删
    for bad in ("../soyo.pth", "sub\\x.pth", "C:/x.pth", "", ".", "..", "x/y.pth"):
        st, d = jpost("/api/models/delete", {"name": bad, "confirm": True})
        chk("非法名被拒 %r" % bad, st == 400 and not d.get("ok"), (st, d.get("msg")))
    st, d = jpost("/api/models/delete", {"name": "不存在.pth", "confirm": True})
    chk("不存在的模型报错", st == 400 and "没有" in d.get("msg", ""), d.get("msg"))
    chk("soyo 仍然完好", os.path.isfile(os.path.join(wdir, "soyo.pth")))

    # 孤儿索引删除
    st, d = jpost("/api/models/delete",
                  {"name": "added_IVF9_Flat_nprobe_1_gone_v2.index", "confirm": True})
    chk("孤儿索引可删", d.get("ok") and not os.path.isfile(
        os.path.join(ldir, "added_IVF9_Flat_nprobe_1_gone_v2.index")), d)

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n== 结果 ==")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
