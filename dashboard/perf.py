# -*- coding: utf-8 -*-
"""设备自适应性能档位。

为什么单独一个模块：
    同一套参数在「8G 显存的游戏本」和「只有核显的办公本」上差别巨大——
    照抄一边的配置，另一边不是白占内存就是白白慢一半。这里只做一件事：
    把硬件探测清楚，给出一个档位（turbo / balanced / lite）和三样东西：

        threads  给 BLAS / torch 的线程数（按物理核，不看逻辑核，SMT 反而拖慢）
        prewarm  启动后要不要在后台「预加载」重模块（省掉首首歌的冷启动）
        说明文本  给面板显示，用户能知道自己在什么档

设计要点：
    · 探测全部走 stdlib（ctypes + nvidia-smi），面板不因此多 import 任何重包；
    · 结果缓存，探测一次到底；`probe(force=True)` 可重探（面板刷新用）；
    · 只给「建议」，不偷偷改用户设置——用户自己调过的并发数不覆盖；
    · 环境变量 RVC_PERF 可强制档位（turbo/balanced/lite），排查问题用。
"""
from __future__ import annotations

import ctypes
import os
import subprocess

CACHE = {}
FORCE_ENV = "RVC_PERF"

# 档位 -> 线程上限。再高不会更快：这些库并行到内存带宽就打满了，
# 反而和推理抢核心（实测 32 线程比 8 线程的 UVR 分离慢）。
THREAD_CAP = {"turbo": 8, "balanced": 6, "lite": 4}


def _mem_mb():
    """(总内存, 可用内存) MB。拿不到就 (-1, -1)。"""
    try:
        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return int(ms.ullTotalPhys / 1048576), int(ms.ullAvailPhys / 1048576)
    except Exception:
        return -1, -1


def physical_cores():
    """物理核数（不是逻辑核）。Windows 上用 GetLogicalProcessorInformation 数。"""
    try:
        # SYSTEM_LOGICAL_PROCESSOR_INFORMATION 在 x64 上是固定 32 字节：
        #   ULONG_PTR Processo​rMask(8) + Relationship(4) + 对齐(4) + 联合体(16)
        # 之前把联合体多写了 8 字节，除以 40 就少算了几个核（16 核算成 12）。
        class _PROCESSOR(ctypes.Structure):
            _fields_ = [("ProcessorMask", ctypes.c_ulonglong),
                        ("Relationship", ctypes.c_uint),
                        ("_pad", ctypes.c_uint),
                        ("_union", ctypes.c_ubyte * 16)]

        size = ctypes.c_ulong(0)
        # RelationProcessorCore = 0
        ctypes.windll.kernel32.GetLogicalProcessorInformation(None, ctypes.byref(size))
        if not size.value:
            raise OSError("no size")
        buf = ctypes.create_string_buffer(size.value)
        ok = ctypes.windll.kernel32.GetLogicalProcessorInformation(
            buf, ctypes.byref(size))
        if not ok:
            raise OSError("call failed")
        count = size.value // ctypes.sizeof(_PROCESSOR)
        arr = ctypes.cast(buf, ctypes.POINTER(_PROCESSOR))
        cores = sum(1 for i in range(count) if arr[i].Relationship == 0)
        if cores > 0:
            return cores
    except Exception:
        pass
    logical = os.cpu_count() or 4
    # 猜：桌面/笔记本带超线程时逻辑核是物理核的 2 倍
    return max(1, logical // 2) if logical >= 4 else logical


def gpu_list():
    """[{name, vram_mb}]。只用 nvidia-smi，不 import torch（面板里导不起）。"""
    try:
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=8, creationflags=flags,
            encoding="utf-8", errors="ignore").stdout or ""
    except Exception:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            try:
                vram = int(float(parts[1]))
            except ValueError:
                vram = 0
            gpus.append({"name": parts[0], "vram_mb": vram})
    return gpus


def probe(force=False):
    """探测一次硬件并给出档位。结果缓存，force=True 重探。"""
    if CACHE and not force:
        return CACHE
    total, avail = _mem_mb()
    phys = physical_cores()
    logical = os.cpu_count() or phys
    gpus = gpu_list()
    vram = max([g["vram_mb"] for g in gpus], default=0)
    forced = (os.environ.get(FORCE_ENV) or "").strip().lower()

    if forced in THREAD_CAP:
        tier = forced
    elif gpus and vram >= 7000 and (total < 0 or total >= 11000):
        tier = "turbo"
    elif gpus:
        tier = "balanced"
    else:
        tier = "lite"

    threads = max(2, min(phys, THREAD_CAP[tier]))

    # 预加载档位：能不能在后台把「第一首歌要付的账」提前付掉。
    # 内存不够时宁可不预载——分离模型常驻要几百 MB，宁可让第一首慢点也别 OOM。
    prewarm = "off"
    if avail < 0:
        prewarm = "import"
    elif avail >= 3500 and tier == "turbo":
        prewarm = "full"          # 导入 + 分离模型一起预载
    elif avail >= 2000:
        prewarm = "uvr"           # 导入 + 分离模型
    elif avail >= 1200:
        prewarm = "import"        # 只把重模块导入做完，不占常驻内存
    if tier == "lite":
        # 纯 CPU：分离模型常驻会跟推理抢内存，只做导入
        prewarm = "import" if avail < 0 or avail >= 1200 else "off"

    CACHE.clear()
    CACHE.update({
        "tier": tier,
        "threads": threads,
        "cpu_physical": phys,
        "cpu_logical": logical,
        "ram_total_mb": total,
        "ram_avail_mb": avail,
        "gpu": gpus,
        "vram_mb": vram,
        "prewarm": prewarm,
        "forced": bool(forced in THREAD_CAP),
    })
    return CACHE


def threads():
    return probe()["threads"]


def prewarm_level():
    return probe()["prewarm"]


def env():
    """给子进程用的线程数环境变量。

    必须**在 numpy / torch 导入之前**生效，否则这几个库已经按默认值起了线程池。
    面板拉起服务时把它们塞进 env，后端自己启动时也会再兜一次。
    """
    n = str(threads())
    return {
        "OMP_NUM_THREADS": n,
        "MKL_NUM_THREADS": n,
        "OPENBLAS_NUM_THREADS": n,
        "NUMEXPR_NUM_THREADS": n,
        "VECLIB_MAXIMUM_THREADS": n,
    }


def apply_to_process():
    """给「自己这个进程」兜底（不在 shell 环境里的时候用）。"""
    for k, v in env().items():
        os.environ.setdefault(k, v)


def child_hints(target):
    """按服务给出「要不要在后台预热」的开关（面板拉起子进程时塞进 env）。

    · 后端 3333：把 torch / 分离模型预载，首首歌不用再等 43 秒；
    · WebUI 7865：预载 rmvpe 并跑一次静音（首曲的 F0 快 30 秒），
      但显存/内存不够时不做，别为了快几秒把显存吃光。
    """
    level = prewarm_level()
    if target == "api":
        return {"RVC_PREWARM": level}
    if target == "webui":
        p = probe()
        ok = (p["tier"] in ("turbo", "balanced")
              and level in ("full", "uvr")
              and (p["ram_avail_mb"] < 0 or p["ram_avail_mb"] >= 2500))
        return {"RVC_WEBUI_PREWARM": "1" if ok else "0"}
    return {}


def describe():
    """一行中文说明，给面板显示。"""
    p = probe()
    name = "无独显（CPU）"
    if p["gpu"]:
        g = p["gpu"][0]
        name = "%s %dG" % (g["name"].replace("NVIDIA ", ""), round(g["vram_mb"] / 1024.0))
    total = round(p["ram_total_mb"] / 1024.0) if p["ram_total_mb"] > 0 else 0
    label = {"turbo": "极速", "balanced": "均衡", "lite": "省内存"}[p["tier"]]
    tail = {"full": "已预加载分离模型", "uvr": "已预加载分离模型",
            "import": "只预热模块", "off": "不预热"}[p["prewarm"]]
    return "%s · %s核/%dG内存 · 档位 %s（%s，%s线程）" % (
        name, p["cpu_physical"], total, label, tail, p["threads"])
