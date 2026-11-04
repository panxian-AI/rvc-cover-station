# -*- coding: utf-8 -*-
"""系统信息采集：端口监听、进程名、GPU、局域网地址、日志尾部。

全部带缓存（netstat 一次几百毫秒，3 秒轮询一次太浪费）。
"""
import ctypes
import json
import os
import re
import socket
import subprocess
import threading
import time

from .core import *          # noqa: F401,F403
from . import core

# ---------------- 系统信息（带缓存） ----------------
_cache = {"netstat": (0, {}), "gpu": (0, None), "ips": (0, []), "log": (0, None)}
_lock = threading.Lock()


def listening_map():
    """{端口: [PID, ...]} —— 同一端口可能有多个监听者。

    为什么不只留一个：Windows 允许两个进程同时监听同一端口（就是 8899
    叠了 4 个实例那种情况）。以前这里用 setdefault 只记第一个，停止时按
    端口找 PID 就只杀得掉一个，剩下的继续占着端口——用户看到的现象就是
    「点了停止，端口还在监听，没法真正退出」。全部收集，才能全部杀掉。
    """
    now = time.time()
    with _lock:
        ts, mp = _cache["netstat"]
        if now - ts < 4 and mp:
            return mp
    mp = {}
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             timeout=8, creationflags=0x08000000).stdout.decode("gbk", "ignore")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[-2] == "LISTENING":
                local = parts[1]
                port = local.rsplit(":", 1)[-1]
                if port.isdigit() and parts[-1].isdigit():
                    pids = mp.setdefault(int(port), [])
                    pid = int(parts[-1])
                    if pid not in pids:
                        pids.append(pid)
    except Exception:
        pass
    with _lock:
        _cache["netstat"] = (now, mp)
    return mp


def proc_name(pid):
    if not pid:
        return ""
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=8,
                             creationflags=0x08000000).stdout.decode("gbk", "ignore")
        first = out.strip().splitlines()[0] if out.strip() else ""
        m = re.match(r'"([^"]+)"', first)
        return m.group(1) if m else ""
    except Exception:
        return ""


def proc_cmdline(pid):
    """进程完整命令行；拿不到返回 ""。

    为什么要自己写：psutil 没装，wmic 在 Win11 24H2 已被移除。
    ProcessCommandLineInformation(=60) 是 Win8.1+ 的信息类，直接返回
    UNICODE_STRING，不用去读目标进程的 PEB（那要先跨进程读内存）。

    这条能力是必需的：WebUI 加载到一半崩掉时进程还在、显存还占着，但
    端口已经没人监听了 —— 只按端口找永远发现不了它。
    """
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        ntdll = ctypes.WinDLL("ntdll")
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long

        h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            need = ctypes.c_ulong(0)
            ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(need))
            if not need.value:
                return ""
            buf = ctypes.create_string_buffer(need.value)
            st = ntdll.NtQueryInformationProcess(h, 60, buf, need.value,
                                                 ctypes.byref(need))
            if st != 0:
                return ""
            length = ctypes.c_ushort.from_buffer(buf).value
            # UNICODE_STRING 在 x64 下：Length(2) + MaxLength(2) + 对齐(4) + Buffer(8)
            ptr = ctypes.c_void_p.from_buffer(buf, 8).value
            if not length or not ptr:
                return ""
            return ctypes.wstring_at(ptr, length // 2)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def python_pids():
    """所有 python.exe / pythonw.exe 的 PID（含 venv 的 shim）。

    用 tasklist 而不是快照 API：它已经在 proc_name 里用着，
    一条命令拿全，省得再写一遍 Toolhelp32 的结构体解析。
    """
    pids = []
    for image in ("python.exe", "pythonw.exe"):
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % image,
                                  "/FO", "CSV", "/NH"],
                                 capture_output=True, timeout=8,
                                 creationflags=0x08000000).stdout.decode("gbk", "ignore")
        except Exception:
            continue
        for line in out.splitlines():
            m = re.match(r'"[^"]+","(\d+)"', line.strip())
            if m:
                pids.append(int(m.group(1)))
    return pids



def gpu_info():
    now = time.time()
    with _lock:
        ts, val = _cache["gpu"]
        if now - ts < 5:
            return val
    val = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=6, creationflags=0x08000000).stdout.decode("utf-8", "ignore")
        f = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        val = {"name": f[0], "util": int(float(f[1])), "mem_used": int(float(f[2])),
               "mem_total": int(float(f[3])), "temp": int(float(f[4]))}
    except Exception:
        val = None
    with _lock:
        _cache["gpu"] = (now, val)
    return val


def _default_ip():
    """默认出口 IP，通常就是手机要连的那个"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def lan_ips():
    """列出所有 IPv4 地址 + 所属网卡名"""
    now = time.time()
    with _lock:
        ts, val = _cache["ips"]
        if now - ts < 10 and val:
            return val
    found = []
    gw = ""
    # 1) ipconfig 解析（能拿到网卡名，中文/英文系统都适用）
    try:
        raw = subprocess.run(["ipconfig"], capture_output=True, timeout=8,
                             creationflags=0x08000000).stdout
        text = raw.decode("gbk", "ignore")
        if "IPv4" not in text and "IPv6" not in text:
            text = raw.decode("utf-8", "ignore")
        iface = ""
        for line in text.splitlines():
            s = line.strip()
            # 只认“XX 适配器 XXX:” 这类行，避免被“连接特定的 DNS 后缀 . . . .:”覆盖
            if s.endswith(":") and not re.search(r"\d+\.\d+\.\d+\.\d+", s) and \
               re.search(r"适配器|adapter|WLAN|Ethernet|以太网|无线", s, re.I):
                iface = s.rstrip(":").strip()
                continue
            # 顺手记下默认网关（热点共享时它就是另一台设备）
            if re.search(r"默认网关|Default Gateway", s, re.I):
                m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", s)
                if m and m.group(1) != "0.0.0.0":
                    gw = m.group(1)
                continue
            # 只取 IPv4 地址行，跳过子网掩码 / DNS，否则会把网关当成自己的 IP
            if not re.search(r"IPv4|IP Address", s, re.I):
                continue
            for ip in re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", s):
                if ip.startswith(("127.", "169.254.", "255.")) or ip == "0.0.0.0":
                    continue
                if ip not in [x["ip"] for x in found]:
                    found.append({"ip": ip, "iface": iface})
    except Exception:
        pass
    # 2) 兜底：getaddrinfo
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith(("127.", "169.254.")) and ip not in [x["ip"] for x in found]:
                found.append({"ip": ip, "iface": ""})
    except Exception:
        pass
    default = _default_ip()
    for x in found:
        x["default"] = (x["ip"] == default)
        x["private"] = bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", x["ip"]))
    found.sort(key=lambda x: (not x["default"], not x["private"]))
    with _lock:
        _cache["ips"] = (now, found)
        _cache["gw"] = (now, gw)
    return found


def gateway_ip():
    lan_ips()  # 顺便刷新
    with _lock:
        return _cache.get("gw", (0, ""))[1]


def newest_api_log():
    try:
        files = [os.path.join(API_DIR, f) for f in os.listdir(API_DIR)
                 if re.match(r"^api_rvc.*\.log$", f)]
        if not files:
            return None
        return max(files, key=os.path.getmtime)
    except Exception:
        return None


def log_tail(n=14):
    p = newest_api_log()
    if not p:
        return {"file": None, "lines": []}
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            data = f.read().decode("utf-8", "ignore")
        lines = [l for l in data.splitlines() if l.strip()][-n:]
        return {"file": os.path.basename(p), "lines": lines}
    except Exception as e:
        return {"file": os.path.basename(p), "lines": ["读取失败: %s" % e]}


def panel_log_tail(n=120):
    """面板自己的控制台输出（静默启动时它落在 dashboard/panel_console.log）。

    静默模式的代价是看不到黑窗口里的启动横幅；有了这个接口，控制台上点开
    「启动日志」就能看到同样的内容，排障不用再去找窗口。
    """
    p = os.path.join(HERE, "panel_console.log")
    if not os.path.isfile(p):
        return {"file": None, "lines": [], "size": 0}
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64000))
            data = f.read().decode("utf-8", "replace")
        lines = [l for l in data.splitlines() if l.strip()][-n:]
        return {"file": os.path.basename(p), "lines": lines, "size": size}
    except Exception as e:
        return {"file": os.path.basename(p), "lines": ["读取失败: %s" % e], "size": 0}


def active_index_from_log():
    """从后端日志里抓当前生效的 index（能顺便验证索引有没有真的接上）"""
    p = newest_api_log()
    if not p:
        return ""
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 20000))
            txt = f.read().decode("utf-8", "ignore")
        hits = re.findall(r"索引[:：]\s*(\S+)", txt)
        return os.path.basename(hits[-1]) if hits else ""
    except Exception:
        return ""


def rvc_models():
    """
    直接从 WebUI 的权重目录读模型列表（比调 gradio 内部 API 稳定，
    gradio 5 的 /run/xxx 路由已变，不同版本路径不一致）
    """
    models = []
    wdir = os.path.join(RVC_ROOT, "assets", "weights")
    if os.path.isdir(wdir):
        models = sorted((f, os.path.getsize(os.path.join(wdir, f)))
                        for f in os.listdir(wdir) if f.lower().endswith(".pth"))
    idx = []
    for base in (os.path.join(RVC_ROOT, "logs"), os.path.join(RVC_ROOT, "assets", "indices")):
        if os.path.isdir(base):
            for f in sorted(os.listdir(base)):
                if f.endswith(".index") and f not in idx:
                    idx.append(f)
    api_up, webui_up = port_open(3333), port_open(7865)
    note = ""
    if not api_up:
        note = "3333 未启动，手机插件无法调用"
    elif not webui_up:
        note = "7865(RVC WebUI) 未启动 —— 3333 收到请求也换不了声，请先启动 WebUI"
    return {
        "online": api_up,
        "webui": webui_up,
        "models": [{"name": n, "mb": round(s / 1048576, 1)} for n, s in models],
        "indices": idx[:8],
        "active_index": active_index_from_log(),
        "note": note,
    }


def collect():
    lm = listening_map()
    ports = []
    for w in WATCH:
        pids = list(lm.get(w["port"]) or [])
        pid = pids[0] if pids else None
        ports.append({
            "port": w["port"], "key": w["key"], "name": w["name"], "desc": w["desc"],
            "open": port_open(w["port"]), "pid": pid, "pids": pids,
            "multi": len(pids) > 1,
            "proc": proc_name(pid) if pid else "",
        })
    ips = lan_ips()
    default_ip = next((x["ip"] for x in ips if x.get("default")), (ips[0]["ip"] if ips else ""))
    # 用局域网 IP 自测 3333 是否可达（这才是手机能不能连的真实依据）
    lan_reach = None
    if default_ip:
        lan_reach = port_open(3333, default_ip, timeout=1.0)
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "ports": ports,
        "ips": ips,
        "default_ip": default_ip,
        "gateway": gateway_ip(),
        "plugin_url": ("http://%s:3333/" % default_ip) if default_ip else "",
        "ncm_url": ("http://%s:3002" % default_ip) if default_ip else "",
        "lan_reach": lan_reach,
        "rvc": rvc_models(),
        "gpu": gpu_info(),
        "ncm_preflight": ncm_preflight(),
        "perf": perf_mod.probe() if perf_mod else {},
        "perf_text": perf_mod.describe() if perf_mod else "",
        "silent": os.environ.get("RVC_PANEL_SILENT") == "1",
        "tray": tray_up(),
        "log": log_tail(),
    }

def pids_on_port(port, fresh=False):
    """端口上**所有**监听 PID（可能不止一个，见 listening_map 的说明）

    fresh=True 时强制重读 netstat。停止操作必须用这个：缓存是 4 秒的，
    刚叠上来的实例会看不见，漏杀就成了「点停止但端口还在监听」。
    """
    if fresh:
        with _lock:
            _cache["netstat"] = (0, {})
    pids = list(listening_map().get(port) or [])
    if not pids:
        # 缓存可能是 4 秒前的，端口刚起来会漏；清一次缓存重查
        with _lock:
            _cache["netstat"] = (0, {})
        pids = list(listening_map().get(port) or [])
    return pids


def ncm_preflight():
    """网易云搜索的配置体检。

    ncmapi 要有 ncm_cookie.txt（含 MUSIC_U）才能真的搜到歌 —— 全新安装
    只有 example 文件，服务能起但搜什么都是空/报错。这里把状态查出来，
    让控制台和一键开启都能提前告诉用户该干什么，而不是等搜索失败再猜。
    """
    proxy = os.path.join(NCM_DIR, "ncm_proxy.py")
    cookie = os.path.join(NCM_DIR, "ncm_cookie.txt")
    has_cookie = False
    try:
        if os.path.isfile(cookie):
            with open(cookie, "r", encoding="utf-8", errors="ignore") as f:
                has_cookie = "MUSIC_U=" in f.read()
    except OSError:
        pass
    return {"installed": os.path.isfile(proxy), "cookie": has_cookie}


def webui_preflight():
    """WebUI 完整性体检：安装不完整时给出说得清的错，而不是 Errno 2。"""
    if not os.path.isdir(RVC_ROOT):
        return False, "没找到 RVC-WebUI 目录（%s）——安装可能没完成，重新双击一键安装" % RVC_ROOT
    if not os.path.isfile(os.path.join(RVC_ROOT, "webui.py")):
        return False, "RVC-WebUI 不完整（缺 webui.py）——重新双击一键安装补齐"
    return True, ""
