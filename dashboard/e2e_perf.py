# -*- coding: utf-8 -*-
"""设备自适应与启动/翻唱提速的回归测试（v1.3.2）。

覆盖三块，全部不碰真 GPU、不加载真模型：
  A. perf.py 的档位判定：显存 / 内存 / 无独显 / 强制档位 四种输入 → 期望档位与线程数
  B. 后端预热与本地模型列表：模型扫描、任务中让路、关掉预热、内存不足跳过
  C. 接线检查：面板给子进程注入线程数与预热开关；/api/status 带档位信息

跑法（必须在真实安装目录下，依赖 RVCSVC-API 的相对位置）：
    apienv\\Scripts\\python.exe e2e_perf.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import perf   # noqa: E402

PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


REAL_MEM, REAL_CORES, REAL_GPU = perf._mem_mb, perf.physical_cores, perf.gpu_list


def fake_hw(mem_total, mem_free, cores, gpus):
    """把硬件探测换成假数据，再重算档位。"""
    perf._mem_mb = lambda: (mem_total, mem_free)
    perf.physical_cores = lambda: cores
    perf.gpu_list = lambda: gpus
    perf.gpu_list.__doc__ = "fake"
    return perf.probe(force=True)


def restore_hw():
    perf._mem_mb, perf.physical_cores, perf.gpu_list = REAL_MEM, REAL_CORES, REAL_GPU
    os.environ.pop("RVC_PERF", None)
    perf.probe(force=True)


def test_tiers():
    print("\n== A. 性能档位判定（假硬件） ==")

    p = fake_hw(15380, 5000, 12, [{"name": "RTX 5060", "vram_mb": 8192}])
    chk("8G 独显 + 15G 内存 → turbo", p["tier"] == "turbo", p["tier"])
    chk("线程数按物理核封顶 8", p["threads"] == 8, p["threads"])
    chk("内存充裕 → 预热到分离模型", p["prewarm"] == "full", p["prewarm"])

    p = fake_hw(16384, 4000, 16, [{"name": "GTX 1650", "vram_mb": 4096}])
    chk("4G 独显 → balanced", p["tier"] == "balanced", p["tier"])
    chk("4G 独显仍只预热到分离模型", p["prewarm"] == "uvr", p["prewarm"])

    p = fake_hw(8192, 3000, 4, [])
    chk("无独显 → lite", p["tier"] == "lite", p["tier"])
    chk("lite 只预热模块（不常驻分离模型）", p["prewarm"] == "import", p["prewarm"])
    chk("4 核机器线程数=4", p["threads"] == 4, p["threads"])

    p = fake_hw(15380, 900, 12, [{"name": "RTX 5060", "vram_mb": 8192}])
    chk("可用内存只有 900MB → 不预热（别 OOM）", p["prewarm"] == "off", p["prewarm"])

    os.environ["RVC_PERF"] = "lite"
    p = perf.probe(force=True)
    chk("RVC_PERF 可强制档位", p["tier"] == "lite" and p["forced"], (p["tier"], p["forced"]))
    del os.environ["RVC_PERF"]

    p = fake_hw(15380, 5000, 12, [{"name": "RTX 5060", "vram_mb": 8192}])
    e = perf.env()
    chk("线程数写进 OMP/MKL/OPENBLAS 四个变量",
        all(e.get(k) == str(p["threads"]) for k in
            ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")), e)

    txt = perf.describe()
    chk("档位说明是人话且含关键信息",
        ("极速" in txt or "均衡" in txt or "省内存" in txt) and "线程" in txt, txt)

    hints = perf.child_hints("api")
    chk("后端拿到 RVC_PREWARM", hints.get("RVC_PREWARM") == p["prewarm"], hints)
    wh = perf.child_hints("webui")
    chk("WebUI 拿到 RVC_WEBUI_PREWARM=1", wh.get("RVC_WEBUI_PREWARM") == "1", wh)

    p = fake_hw(15380, 1500, 12, [{"name": "RTX 5060", "vram_mb": 8192}])
    wh = perf.child_hints("webui")
    chk("内存紧张时 WebUI 不预热 rmvpe", wh.get("RVC_WEBUI_PREWARM") == "0", wh)

    restore_hw()
    chk("恢复真硬件后档位仍可用", perf.probe()["tier"] in ("turbo", "balanced", "lite"))


def test_backend():
    print("\n== B. 后端：本地模型列表 / 预热让路 ==")
    src = open(os.path.join(HERE, "..", "RVCSVC-API", "app_rvc.py"),
               encoding="utf-8").read().replace("\r\n", "\n")

    # --- 本地模型扫描 ---
    a = src.index("def _local_models")
    b = src.index("def _collect_indices")
    tmp = tempfile.mkdtemp(prefix="rvc_perf_")
    wdir = os.path.join(tmp, "assets", "weights")
    os.makedirs(wdir)
    for n in ("b.pth", "a.pth", "note.txt"):
        open(os.path.join(wdir, n), "w").close()
    ns = {"os": os, "RVC_ROOT": tmp}
    exec(compile(src[a:b], "local_models", "exec"), ns)
    got = ns["_local_models"]()
    chk("本地扫到 2 个 .pth 且忽略非模型文件", got == ["a.pth", "b.pth"], got)
    ns["RVC_ROOT"] = os.path.join(tmp, "nope")
    chk("目录不存在时返回空表（不抛异常）", ns["_local_models"]() == [])

    # --- 任务让路 ---
    a = src.index('_jobs = {"last": 0.0}')
    b = src.index("def prewarm(")
    ns2 = {"os": os, "time": time, "_threading": __import__("threading"), "print": print}
    exec(compile(src[a:b], "jobs", "exec"), ns2)
    chk("刚起完服务时不算忙", ns2["_job_active"]() is False)
    ns2["_mark_job"]()
    chk("标记任务后 3 分钟内算忙", ns2["_job_active"]() is True)
    chk("6 分钟前的任务不算忙", ns2["_job_active"](window=0.0) is False)

    # --- 预热：关掉 / 内存不足 / 正常 ---
    a = src.index("def prewarm(")
    b = src.index("def _start_prewarm_thread")
    calls = []
    ns3 = {
        "os": os, "time": time, "print": print,
        "_job_active": lambda: False,
        "_avail_mem_mb": lambda: 5000.0,
        "_get_torch": lambda: calls.append("torch") or object(),
        "get_pre_fun": lambda: calls.append("uvr"),
        "__builtins__": __builtins__,
    }
    # 预热里会 `from uvr5.vr import AudioPre`（真模块要连带 torch，测试里换成假的）
    import types
    fake_uvr5 = types.ModuleType("uvr5")
    fake_vr = types.ModuleType("uvr5.vr")
    fake_vr.AudioPre = object
    fake_uvr5.vr = fake_vr
    saved = {k: sys.modules.get(k) for k in ("uvr5", "uvr5.vr")}
    sys.modules["uvr5"] = fake_uvr5
    sys.modules["uvr5.vr"] = fake_vr
    try:
        exec(compile(src[a:b], "prewarm", "exec"), ns3)
        os.environ["RVC_PREWARM"] = "off"
        chk("RVC_PREWARM=off 直接不热身", ns3["prewarm"]() is False and not calls)
        chk("off 时不加载分离模型", "uvr" not in calls)
        os.environ.pop("RVC_PREWARM", None)

        ns3["_avail_mem_mb"] = lambda: 600.0
        chk("可用内存 600MB 时跳过预热", ns3["prewarm"]("full") is False and not calls)

        ns3["_avail_mem_mb"] = lambda: 5000.0
        chk("内存够时预热成功", ns3["prewarm"]("import") is True and calls == ["torch"], calls)
        calls.clear()
        chk("level=uvr 会加载分离模型",
            ns3["prewarm"]("uvr") is True and calls == ["torch", "uvr"], calls)

        ns3["_job_active"] = lambda: True
        calls.clear()
        chk("有任务在跑时预热让路", ns3["prewarm"]("uvr") is False and not calls)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # --- 源码级约束（防止将来又把这笔账搬回启动路径） ---
    chk("启动不再同步 show_model()",
        "initial_models = _local_models()" in src and "initial_models = show_model()\nif" not in src)
    chk("伴奏变调有并行入口", "_start_inst_shift(" in src and "_finish_inst_shift(" in src)
    chk("convert 会打阶段耗时", "本首耗时" in src)
    chk("线程数在 numpy 之前兜底", src.index("OMP_NUM_THREADS") < src.index("import gradio"))

    p = os.path.join(HERE, "..", "..", "RVC-WebUI", "infer", "vc", "pipeline.py")
    psrc = open(p, encoding="utf-8").read().replace("\r\n", "\n")
    chk("rmvpe 已是模块级缓存（切模型不再重载）",
        "_RMVPE_CACHE" in psrc and "def get_rmvpe" in psrc and "def prewarm_rmvpe" in psrc)
    msrc = open(os.path.join(HERE, "..", "..", "RVC-WebUI", "infer", "vc", "modules.py"),
                encoding="utf-8").read().replace("\r\n", "\n")
    chk("同一个模型重复切会短路", "_loaded_person" in msrc and "跳过）" in msrc)
    wsrc = open(os.path.join(HERE, "..", "..", "RVC-WebUI", "webui.py"),
                encoding="utf-8").read().replace("\r\n", "\n")
    chk("WebUI 预热受环境变量控制", "RVC_WEBUI_PREWARM" in wsrc and "_start_rmvpe_prewarm" in wsrc)


def test_wiring():
    print("\n== C. 面板接线 ==")
    psrc = open(os.path.join(HERE, "panel.py"), encoding="utf-8").read().replace("\r\n", "\n")
    chk("拉起服务时注入线程数", "perf_mod.env()" in psrc)
    chk("拉起服务时注入预热开关", "perf_mod.child_hints(target)" in psrc)
    chk("/api/status 带档位信息", '"perf": perf_mod.probe()' in psrc and '"perf_text"' in psrc)
    html = open(os.path.join(HERE, "console_page.html"), encoding="utf-8").read()
    chk("控制台有档位显示位", 'id="perfLine"' in html and "性能档位" in html)


def main():
    print("RVC 性能档位 / 预热 回归测试")
    test_tiers()
    test_backend()
    test_wiring()
    print("\n== 结果 ==")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
