#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RVC 状态监控面板
- 显示 3333(翻唱后端) / 7865(RVC WebUI) / 8899(本面板) 的端口状态
- 显示当前所有局域网 IP，并给出手机插件该填的地址（换网络后 IP 会变）
- 显示 RVC 模型列表、GPU 占用、后端日志尾部
- 支持一键启动 / 停止 / 重启两个后端（走 shell 启动，进程独立，不怕面板退出）
只用标准库，任何 Python 3.8+ 都能跑。
"""
import ctypes
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PORT = int(os.environ.get("RVC_PANEL_PORT") or 8899)
API_BASE = "http://127.0.0.1:3333"
WEBUI_BASE = "http://127.0.0.1:7865"
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 路径全部由 paths.py 按「本文件位置」推导，不写死盘符（整栈可搬去移动硬盘）
from paths import (API_BAT, API_DIR, COVERS_DIR, NCM_BAT, NCM_DIR, PROJ,  # noqa: E402
                   PY_API, PY_WEB, RVC_ROOT, STACK, WEBUI_BAT)

# 翻唱页（搜歌 / 选模型 / 成品库 / 缓存管理），同端口挂在 /cover
try:
    import cover as cover_mod
except Exception as _e:      # 缺文件时面板照常可用，只是没有翻唱页
    cover_mod = None
    print("⚠️ 翻唱页未加载（cover.py 导入失败）: %s" % _e)

# 模型导入（本机控制台选 Windows 文件 → 识别 RVC 声线 → 装进 WebUI）
try:
    import model_import
except Exception as _e:
    model_import = None
    print("⚠️ 模型导入未加载（model_import.py 导入失败）: %s" % _e)

# 高级设置（同时制作数量 + 翻唱参数），控制台读写
try:
    import settings as settings_mod
except Exception as _e:
    settings_mod = None
    print("⚠️ 高级设置未加载（settings.py 导入失败）: %s" % _e)

# 版本检测 / 在线更新（控制台打开时提示有新版本，可一键更新代码）
try:
    import updater
except Exception as _e:
    updater = None
    print("⚠️ 版本检测未加载（updater.py 导入失败）: %s" % _e)

# 模型市场：搜公开模型站的免费 RVC 声线 → 多线路下载 → 核验 → 装进 WebUI
try:
    import market
except Exception as _e:
    market = None
    print("⚠️ 模型市场未加载（market.py 导入失败）: %s" % _e)

# 访问控制：管理功能**只认本机直连**（回环且不经反代），没有密码、没有远程管理。
# 公网隧道、局域网、带转发头的请求一律拒绝；访客只能看访客页。
MSG_LOCAL_ONLY = "管理功能只在这台电脑上可用：请在运行面板的电脑上打开 http://127.0.0.1:%d/" % PORT

# 安全事件日志：谁试图从远程碰管理接口（被扫/被爆破时能事后查）
SEC_LOG = os.path.join(HERE, "security.log")
_sec_lock = threading.Lock()
_sec_last = {"ip": None, "t": 0.0}

# ---------------- 公网暴露面（fail-closed） ----------------
# 公网（隧道）能碰到的**只有**下面这些路径，其余一律当不存在处理并记一次探测。
# 为什么用白名单而不是黑名单：以后再加管理接口/调试接口时，忘了加保护也不会
# 自动暴露到隧道外面 —— 新路由默认是「公网不可见」。
PUBLIC_GET_EXACT = {"/", "/index.html", "/guest", "/console", "/favicon.ico",
                     "/admin", "/login", "/logout"}   # 后三个只做 302 回访客页
PUBLIC_GET_PREFIX = ("/api/guest/",)
PUBLIC_POST_EXACT = {"/api/guest/request", "/api/guest/wl_apply"}

# 扫描/越权探测：同一 IP 10 分钟内碰 20 次非公开路径 → 封 30 分钟。
# 隧道地址一旦被扫到，全世界的小爬虫都会来试 /admin、/.env、/api/admin/...；
# 光回 403 太客气了（还会白烧 CPU），直接断一段时间。
PROBE_WINDOW = 600
PROBE_LIMIT = 20
PROBE_BAN = 1800
_probe = {}                       # ip -> [时间戳]
_ban = {}                         # ip -> 解封时间戳
_probe_lock = threading.Lock()
_probe_stat = {"total": 0}
_probe_recent = []                # [(时间戳, ip, 路径)]，最近 30 条，给控制台看

# 公网访问总开关：被刷爆 / 不想让人用了，控制台一键暂停，访客只看到维护页。
PUBLIC_STATE = (os.environ.get("RVC_PUBLIC_STATE")
                or os.path.join(HERE, "public_access.json"))
_pub_lock = threading.Lock()
_pub_cache = {"v": None}


def public_state():
    """公网访问开关的完整状态（给控制台看）。"""
    with _pub_lock:
        if _pub_cache["v"] is None:
            st = {"paused": False, "at": 0, "note": ""}
            try:
                with open(PUBLIC_STATE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    st = {"paused": bool(d.get("paused")), "at": float(d.get("at") or 0),
                          "note": str(d.get("note") or "")[:120]}
            except Exception:
                pass
            _pub_cache["v"] = st
        return dict(_pub_cache["v"])


def public_paused():
    """公网访客是否被暂停（本机控制台不受影响）。注意返回 bool。"""
    return bool(public_state().get("paused"))


def set_public_paused(flag, note=""):
    st = {"paused": bool(flag), "at": time.time(), "note": str(note or "")[:120]}
    with _pub_lock:
        try:
            tmp = PUBLIC_STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, PUBLIC_STATE)
        except OSError as e:
            return {"ok": False, "msg": "保存失败：%s" % e}
        _pub_cache["v"] = st
    sec_log("公网访问%s（站长操作）" % ("已暂停" if flag else "已恢复"))
    return {"ok": True, "paused": st["paused"],
            "msg": "已暂停公网访问：访客打开只会看到维护提示，本机控制台不受影响"
                   if flag else "已恢复公网访问"}


def _ban_left(ip):
    if not ip:
        return 0
    with _probe_lock:
        until = _ban.get(ip) or 0
        left = until - time.time()
        if left <= 0:
            _ban.pop(ip, None)
            return 0
        return int(left)


def _probe_hit(ip, path):
    """记一次「公网碰了不该碰的路径」。返回 (是否已封, 封禁剩余秒数)。"""
    if not ip:
        return False, 0
    now = time.time()
    with _probe_lock:
        _probe_stat["total"] += 1
        _probe_recent.append((now, ip, str(path or "")[:80]))
        del _probe_recent[:-30]
        hits = [t for t in _probe.get(ip, []) if now - t < PROBE_WINDOW]
        hits.append(now)
        _probe[ip] = hits
        if len(_probe) > 5000:
            for k in list(_probe)[:2500]:
                _probe.pop(k, None)
        if len(hits) >= PROBE_LIMIT:
            _ban[ip] = now + PROBE_BAN
            _probe[ip] = []
            banned = True
        else:
            banned = _ban.get(ip, 0) > now
        left = int(max(0, _ban.get(ip, 0) - now))
    if banned:
        sec_log("公网探测触发封禁 ip=%s path=%s 封 %d 分钟" % (ip, path, PROBE_BAN // 60), ip="")
    return banned, left


def security_view():
    """控制台「安全」卡片的数据。"""
    now = time.time()
    with _probe_lock:
        banned = [{"ip": k, "left": int(v - now)}
                  for k, v in _ban.items() if v > now]
        banned.sort(key=lambda x: -x["left"])
        recent = [{"t": time.strftime("%H:%M:%S", time.localtime(t)), "ip": i,
                   "path": p} for (t, i, p) in reversed(_probe_recent[-12:])]
        total = _probe_stat["total"]
        tracked = len(_probe)
    st = public_state()
    return {"ok": True, "paused": st["paused"], "paused_at": st["at"], "note": st["note"],
            "banned": banned[:20], "banned_count": len(banned),
            "probe_total": total, "tracked": tracked, "recent": recent,
            "public_paths": sorted(PUBLIC_GET_EXACT - {"/favicon.ico"}),
            "limits": {"probe_window": PROBE_WINDOW, "probe_limit": PROBE_LIMIT,
                       "ban_minutes": PROBE_BAN // 60}}



def sec_log(line, ip=""):
    """追加一行安全日志；文件超过 512KB 就只留后半段，避免被刷爆磁盘。

    同一来源 5 秒内只记一条：否则被人拿脚本狂打管理接口时，每一发请求都同步
    写一次盘，日志反倒成了新的放大器。
    """
    now = time.time()
    with _sec_lock:
        if ip and ip == _sec_last["ip"] and now - _sec_last["t"] < 5:
            return
        _sec_last["ip"], _sec_last["t"] = ip, now
    try:
        if os.path.isfile(SEC_LOG) and os.path.getsize(SEC_LOG) > 512 * 1024:
            with open(SEC_LOG, "r", encoding="utf-8", errors="replace") as f:
                f.seek(-256 * 1024, os.SEEK_END)
                keep = f.read()
            with open(SEC_LOG, "w", encoding="utf-8") as f:
                f.write(keep)
        with open(SEC_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + line + "\n")
    except OSError:
        pass

# 访客点歌页（/，公开）
try:
    import guest as guest_mod
except Exception as _e:
    guest_mod = None
    print("⚠️ 访客页未加载（guest.py 导入失败）: %s" % _e)

WATCH = [
    {"port": 3333, "key": "api", "name": "翻唱后端 RVCSVC-API", "desc": "插件真正调用的服务"},
    {"port": 7865, "key": "webui", "name": "RVC WebUI", "desc": "换声推理引擎，3333 依赖它"},
    {"port": 3002, "key": "ncm", "name": "网易云搜索 API（本地）", "desc": "插件把歌名换成歌曲 ID 用"},
    {"port": PORT, "key": "panel", "name": "本监控面板", "desc": "就是这个页面"},
]

# 不经过系统 HTTP 代理（否则 127.0.0.1 会被代理截走返回 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, payload=None, timeout=6):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def http_code(url, timeout=4):
    try:
        req = urllib.request.Request(url, method="GET")
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return "ERR:%s" % type(e).__name__


def port_open(port, host="127.0.0.1", timeout=0.6):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


# ---------------- 系统信息（带缓存） ----------------
_cache = {"netstat": (0, {}), "gpu": (0, None), "ips": (0, []), "log": (0, None)}
_lock = threading.Lock()


def listening_map():
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
                if port.isdigit():
                    mp.setdefault(int(port), int(parts[-1]))
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
        pid = lm.get(w["port"])
        ports.append({
            "port": w["port"], "key": w["key"], "name": w["name"], "desc": w["desc"],
            "open": port_open(w["port"]), "pid": pid, "proc": proc_name(pid) if pid else "",
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
        "log": log_tail(),
    }


# ---------------- 启停 ----------------
# 记录本面板亲手拉起的子进程 PID。
# 为什么需要：后端刚启动时端口还没监听（约 20 秒），
# 这时点「一键退出」按端口找不到 PID，进程会被漏掉变成孤儿。
_SPAWNED = {}
_spawn_lock = threading.Lock()


def pid_alive(pid):
    """进程是否还活着（不依赖 tasklist，纯 ctypes）"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def pids_on_port(port):
    lm = listening_map()
    pid = lm.get(port)
    if not pid:
        with _lock:
            _cache["netstat"] = (0, {})
        pid = listening_map().get(port)
    return pid


SERVICES = {
    "api": {
        "exe": PY_API,
        "cwd": API_DIR,
        "args": ["-u", "app_rvc.py"],
        "env": {"PYTHONIOENCODING": "utf-8", "NO_PROXY": "localhost,127.0.0.1,::1",
                "PATH": API_DIR + os.pathsep + os.environ.get("PATH", "")},
        "log": os.path.join(API_DIR, "api_rvc_panel.log"),
        "bat": API_BAT,
    },
    "webui": {
        "exe": PY_WEB,
        "cwd": RVC_ROOT,
        "args": ["webui.py", "--port", "7865", "--pycmd", PY_WEB],
        "env": {"PYTHONPATH": RVC_ROOT, "PYTHONSAFEPATH": "1",
                "GRADIO_ANALYTICS_ENABLED": "False",
                "NO_PROXY": "localhost,127.0.0.1,::1"},
        "log": os.path.join(RVC_ROOT, "logs", "webui_service.log"),
        "bat": WEBUI_BAT,
    },
    "ncm": {
        "exe": PY_API,
        "cwd": NCM_DIR,
        "args": ["-u", "ncm_proxy.py"],
        "env": {"PYTHONIOENCODING": "utf-8", "NO_PROXY": "localhost,127.0.0.1,::1"},
        "log": os.path.join(NCM_DIR, "ncm.log"),
        "bat": NCM_BAT,
    },
}


def start(target):
    cfg = SERVICES.get(target)
    if not cfg:
        return False, "未知服务: %s" % target
    if os.path.isfile(cfg["exe"]):
        env = os.environ.copy()
        env.update(cfg["env"])
        try:
            log = open(cfg["log"], "ab", buffering=0)
            log.write(("\n\n===== %s 由控制台启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
            cmd = [cfg["exe"]] + cfg["args"]
            kwargs = dict(cwd=cfg["cwd"], env=env, stdin=subprocess.DEVNULL,
                          stdout=log, stderr=subprocess.STDOUT, close_fds=True)
            try:
                # 优先彻底脱离当前任务对象，关掉面板后子进程也能活
                proc = subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200 | 0x01000000, **kwargs)
            except OSError as e:
                # 面板本身处在不允许 breakaway 的 job 里时会 WinError 5，
                # 去掉 breakaway 只要 DETACHED_PROCESS 一样能起（子进程随 job 走）
                if getattr(e, "winerror", None) != 5:
                    raise
                proc = subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, **kwargs)
                print("breakaway 被拒绝，已用 DETACHED_PROCESS 降级启动 %s" % target)
            with _spawn_lock:
                _SPAWNED[target] = proc.pid
            return True, "已启动 %s，约 30~60 秒后端口就绪" % target
        except Exception as e:
            return False, "启动失败: %s" % e
    # 兜底：直接跑 bat
    if os.path.isfile(cfg["bat"]):
        try:
            os.startfile(cfg["bat"])
            return True, "已通过脚本启动 %s（若几十秒后端口仍关闭，请手动双击 %s）" % (
                target, os.path.basename(cfg["bat"]))
        except Exception as e:
            return False, "启动失败: %s" % e
    return False, "找不到解释器或启动脚本"


def start_all():
    """先起 WebUI 等它就绪，再起后端 —— 顺序反了 3333 会拿不到模型列表。
    已经在跑的服务会跳过，不会重复开局端口冲突。"""
    notes = []

    # 1) WebUI
    if port_open(7865):
        notes.append("WebUI 已在运行")
    else:
        ok1, m1 = start("webui")
        if not ok1:
            return False, "WebUI 启动失败: %s" % m1
        notes.append("WebUI 启动中")

    # 2) 网易云搜索，和 WebUI 加载并行，不冲突
    if port_open(3002):
        notes.append("网易云已在运行")
    else:
        ok_n, m_n = start("ncm")
        notes.append("网易云已启动" if ok_n else "网易云启动失败: %s" % m_n)

    # 3) 等 WebUI 端口就绪
    if not port_open(7865):
        for _ in range(60):  # 最多等 120 秒
            if port_open(7865):
                break
            time.sleep(2)
        else:
            return False, "WebUI 120 秒内没起来，请到 RVC 窗口看报错"
        time.sleep(2)

    # 4) 后端
    if port_open(3333):
        notes.append("后端已在运行")
        return True, "；".join(notes)

    ok2, m2 = start("api")
    if not ok2:
        return False, "WebUI 已就绪，但后端启动失败: %s" % m2
    # 等后端真正监听再返回，否则用户以为开好了其实还没好
    t0 = time.time()
    while time.time() - t0 < 90:
        if port_open(3333):
            notes.append("后端已就绪")
            return True, "；".join(notes)
        time.sleep(1.5)
    return False, "后端进程已启动，但 90 秒内 3333 没就绪，请到「后端日志」看报错"


# 停止顺序：先后端，再网易云，最后 WebUI
# （反过来 WebUI 先死，后端拿不到模型列表会刷一屏报错）
STOP_ORDER = [("api", 3333), ("ncm", 3002), ("webui", 7865)]


def stop_all(wait=10):
    """停掉三个服务并等端口真正释放。返回 (ok, msg)"""
    parts = []
    all_ok = True
    for name, port in STOP_ORDER:
        with _spawn_lock:
            spawned = _SPAWNED.get(name)
        # 端口在监听，或有自己拉起但还没监听的子进程 → 都要停
        if not port_open(port) and not (spawned and pid_alive(spawned)):
            parts.append("%s 未运行" % name)
            continue
        ok, m = stop(name)
        if not ok:
            all_ok = False
        parts.append(m)
        # 等端口释放，不然后面重启会撞端口
        t0 = time.time()
        while time.time() - t0 < wait and port_open(port):
            time.sleep(0.3)
        if port_open(port):
            parts.append("%s 端口 %d 仍未释放" % (name, port))
            all_ok = False
    return all_ok, "；".join(parts)


def stop(target):
    port = {"api": 3333, "webui": 7865, "ncm": 3002}.get(target)
    if not port:
        return False, "未知服务: %s" % target

    pid = pids_on_port(port) if port_open(port) else None
    how = "端口 %d" % port
    if not pid:
        # 端口还没起来（刚启动 20 秒内点退出）——用自己拉起的 PID
        with _spawn_lock:
            pid = _SPAWNED.get(target)
        if pid and not pid_alive(pid):
            pid = None
        if pid:
            how = "子进程记录"
    if not pid:
        return False, "%s 未在运行，无需停止" % target

    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10, creationflags=0x08000000)
        with _lock:
            _cache["netstat"] = (0, {})
        with _spawn_lock:
            _SPAWNED.pop(target, None)
        # 等它真的死掉
        t0 = time.time()
        while time.time() - t0 < 6 and pid_alive(pid):
            time.sleep(0.25)
        return True, "已停止 %s (PID %d，据%s)" % (target, pid, how)
    except Exception as e:
        return False, "停止 %s 失败: %s" % (target, e)


# ---------------- 公网隧道信息 ----------------
def public_info():
    """读 tunnel.py 写下的公网地址，并判断 cloudflared 是否还在跑。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "公网链接.txt")
    url = ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        for line in reversed(txt.splitlines()):
            line = line.strip()
            if line.startswith("http"):
                url = line
                break
    except OSError:
        pass
    alive = False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
                             capture_output=True, timeout=6).stdout or ""
        alive = "cloudflared" in out.decode("gbk", "ignore").lower()
    except Exception:
        pass
    return {"ok": True, "url": url, "alive": alive}


# ---------------- HTTP 服务 ----------------
# 同时处理的连接上限。ThreadingHTTPServer 是「一连接一线程、不设上限」，
# 公网被人开几百个慢连接（连上来不发完整请求，卡满 timeout）就能把线程和内存耗光，
# 正经访客反而打不开。这里用信号量卡住上限，超出的直接回 503。
MAX_CONN = 96


class PanelServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(MAX_CONN)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Retry-After: 5\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()          # 线程没起来，槽位要还回去
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 20             # 慢连接/半开连接不许一直占线程（Slowloris）

    def log_message(self, *a):
        pass

    # 统一安全响应头：防点击劫持、防 MIME 嗅探、限制外链/内联注入面
    SEC_HEADERS = [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
        ("Content-Security-Policy",
         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
         "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
         "base-uri 'none'; form-action 'self'"),
    ]

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self.SEC_HEADERS:
            self.send_header(k, v)
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where):
        """302 跳到别处（不带查询串，避免把参数粘出去）。"""
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in self.SEC_HEADERS:
            self.send_header(k, v)
        self.end_headers()

    def _send_file(self, path):
        """直接把 mp3 吐出去（成品试听 / 下载）"""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            # 不回显绝对路径/系统错误，避免泄露服务器目录结构
            self._send(404, json.dumps({"ok": False, "msg": "文件不存在"}, ensure_ascii=False))
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 访问控制（公网穿透后必须） ----------
    def _client(self):
        """返回 (真实IP, 是否经反代)。

        cloudflared 连本地时来源恒为 127.0.0.1，真实 IP 只在 CF-Connecting-IP /
        X-Forwarded-For 里。**只有连接本身来自回环时才信任这些头**：否则局域网里
        的机器可以随便伪造头冒充任意 IP（绕过登录失败锁定）。
        """
        peer = (self.client_address[0] or "").replace("::ffff:", "")
        if peer in ("127.0.0.1", "::1"):
            # 除了真实 IP，也认 Cloudflare 自己加的几个头：万一哪天 CF-Connecting-IP
            # 被前置代理剥掉，只要还留着 CF-Ray / CF-Visitor 就不会被当成「本机直连」。
            for h in ("CF-Connecting-IP", "X-Forwarded-For", "CF-Ray",
                      "CF-Visitor", "X-Real-IP"):
                v = self.headers.get(h)
                if v:
                    return v.split(",")[0].strip() or "0.0.0.0", True
        return peer or "0.0.0.0", False

    def _host_ok(self):
        """Host 头必须是本机名/回环/本机网卡 IP。

        为什么查这个：防 DNS rebinding —— 恶意站点把域名解析到 127.0.0.1，
        浏览器发来的请求 peer 是回环、又没有转发头，不查 Host 的话会被当成站长，
        网页里的 JS 就能读管理接口、点「一键更新」。隧道域名不在白名单里，
        但它本来就带转发头、走不到本机判定。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            base = host.split("]")[0] + "]"
            rest = host[len(base):]
            port = rest[1:] if rest.startswith(":") else ""
        else:
            base, _, port = host.partition(":")
        if port and not port.isdigit():
            return False
        if base in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return True
        try:
            for x in lan_ips():
                if base and base == str(x.get("ip") or "").lower():
                    return True
        except Exception:
            pass
        return False

    def _is_local(self):
        """是否本机直连：回环地址、**没有**任何转发头、且 Host 是本机地址。

        cloudflared 连本地时来源也是 127.0.0.1，所以必须同时看有没有转发头，
        否则公网来客会被误判成站长。Host 校验再加一道，防 DNS rebinding。
        """
        ip, proxied = self._client()
        return (not proxied) and ip in ("127.0.0.1", "::1") and self._host_ok()

    def _local_only(self, as_json=True):
        """True=放行；False=已回 403。

        管理功能只认本机直连。公网（隧道）、局域网、以及带了转发头的本机脚本
        一律拒绝 —— 没有密码这层，也就没有「远程登进来管理」这回事。
        """
        if self._is_local():
            return True
        ip, _ = self._client()
        sec_log("拒绝远程管理 ip=%s path=%s ua=%s" % (
            ip, (self.path or "")[:120], (self.headers.get("User-Agent") or "")[:100]), ip=ip)
        if as_json:
            self._send(403, json.dumps({"ok": False, "local_only": True,
                                        "msg": MSG_LOCAL_ONLY}, ensure_ascii=False))
        else:
            self._send(403, MSG_LOCAL_ONLY, "text/plain; charset=utf-8")
        return False

    def _maintenance(self, as_json=False):
        """公网访问被站长暂停时的统一响应（本机控制台不受影响）。"""
        if as_json:
            self._send(503, json.dumps({
                "ok": False, "paused": True,
                "msg": "站长暂时关闭了公网访问，请稍后再来"}, ensure_ascii=False))
            return
        html = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>维护中</title><style>body{background:#0f1420;color:#e8eefc;"
                "font-family:-apple-system,'Microsoft YaHei',sans-serif;display:flex;"
                "align-items:center;justify-content:center;height:100vh;margin:0;padding:20px}"
                ".b{background:#182031;border:1px solid #2a3550;border-radius:16px;"
                "padding:26px 28px;max-width:420px;text-align:center;line-height:1.8}"
                "h1{font-size:17px;margin:0 0 8px}p{margin:0;color:#9fb0cc;font-size:13px}"
                "</style></head><body><div class=\"b\"><h1>🛠 暂时关闭中</h1>"
                "<p>站长暂停了公网访问，稍后再来试试。</p></div></body></html>")
        self._send(503, html, "text/html; charset=utf-8")

    def _csrf_ok(self):
        """跨站请求防护：带了 Origin 就必须和本站同源。

        浏览器发起的跨站 POST 一定带 Origin；curl / 本机脚本不带，因此不受影响。
        配合 Cookie 的 SameSite=Lax，构成双保险。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            o = urllib.parse.urlparse(origin)
        except Exception:
            return False
        return bool(o.netloc) and o.netloc.strip().lower() == host

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        ip, proxied = self._client()
        local = self._is_local()

        # ---- 公网第一道闸（fail-closed）：封禁 → 暂停 → 路径白名单 ----
        if not local:
            left = _ban_left(ip)
            if left:
                self._send(403, json.dumps({
                    "ok": False, "msg": "这个网络访问过于频繁，已被临时限制（%d 分钟后自动解除）"
                                        % max(1, left // 60)}, ensure_ascii=False))
                return
            if public_paused():
                self._maintenance(as_json=path.startswith("/api/"))
                return
            if not (path in PUBLIC_GET_EXACT or path.startswith(PUBLIC_GET_PREFIX)):
                # 既不是公开路径 → 记一次探测，再按「管理功能只在本机」拒掉
                _probe_hit(ip, path)
                self._local_only(as_json=path.startswith("/api/"))
                return
            # 明确拒绝路径穿越/危险字符（正常访客不会这么请求）
            if ".." in path or "%2e" in path.lower() or "\x00" in path:
                _probe_hit(ip, path)
                self._send(400, json.dumps({"error": "bad request"}))
                return

        if guest_mod:
            gid, new_gid = guest_mod.gid_from(self.headers.get("Cookie"))
        else:
            gid, new_gid = "", False

        # ---- 首页：本机是控制台，公网/局域网来客只能看访客页 ----
        if path in ("/", "/index.html", "/console"):
            if local:
                self._send(200, CONSOLE_PAGE, "text/html; charset=utf-8")
                return
            if path == "/console":
                self._redirect("/")           # 外面打开 /console 就回访客页
                return
            # 公网来客：只能看到访客点歌页
            if guest_mod:
                guest_mod.touch(gid, ip, self.headers.get("CF-IPCountry"),
                                self.headers.get("User-Agent"))
                hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
                self._send(200, guest_mod.page(), "text/html; charset=utf-8", hdr)
            else:
                self._send(500, "访客页未加载：guest.py 导入失败", "text/plain; charset=utf-8")
            return
        # 访客页固定入口（本机也能预览；公网也是这个页面）
        if guest_mod and path == "/guest":
            if not local:
                guest_mod.touch(gid, ip, self.headers.get("CF-IPCountry"),
                                self.headers.get("User-Agent"))
            hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
            self._send(200, guest_mod.page(), "text/html; charset=utf-8", hdr)
            return
        if guest_mod and path == "/api/guest/file":
            rid = (q.get("rid") or [""])[0]
            alt = ((q.get("gid") or [""])[0] if isinstance(q.get("gid"), list)
                   else (q.get("gid") or ""))
            p = guest_mod.file_for(gid, rid, alt_gid=alt)
            if p:
                self._send_file(p)
            else:
                self._send(404, json.dumps({"ok": False, "msg": "还没有可下载的成品"},
                                           ensure_ascii=False))
            return
        if guest_mod and path.startswith("/api/guest/"):
            code, obj = guest_mod.handle_get(path, q, gid, False, ip)
            # 新访客直接打接口（或 Cookie 失效）也要把访客 id 种下去，
            # 否则验证码绑定的 gid 每次都变，永远过不了
            hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
            self._send(code, json.dumps(obj, ensure_ascii=False), headers=hdr)
            return

        # 旧的密码入口已经取消，书签跳回来即可
        if path in ("/login", "/logout", "/admin"):
            self._redirect("/")
            return

        # ---- 以下全是管理功能：只认本机直连 ----
        if not self._local_only(as_json=path.startswith("/api/")):
            return

        if path in ("/panel",):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/cover":
            if not cover_mod:
                self._send(500, "翻唱页未加载：cover.py 导入失败，看面板控制台输出",
                           "text/plain; charset=utf-8")
            else:
                try:
                    self._send(200, cover_mod.page(), "text/html; charset=utf-8")
                except Exception as e:
                    self._send(500, "翻唱页读取失败: %s" % e, "text/plain; charset=utf-8")
        elif path == "/api/cover/file":
            name = (q.get("name") or [""])[0]
            p = cover_mod.file_abs(name) if cover_mod else None
            if p:
                self._send_file(p)
            else:
                self._send(404, json.dumps({"ok": False, "msg": "文件不存在"}, ensure_ascii=False))
        elif cover_mod and path.startswith("/api/cover/"):
            try:
                code, obj = cover_mod.handle_get(path, q)
            except Exception as e:
                code, obj = 500, {"ok": False, "msg": str(e)}
            self._send(code, json.dumps(obj, ensure_ascii=False))
        elif path == "/market":
            self._send(200, MARKET_PAGE, "text/html; charset=utf-8")
        elif market and path.startswith("/api/market/"):
            code, obj = market.handle_get(path, q)
            self._send(code, json.dumps(obj, ensure_ascii=False))
        elif settings_mod and path == "/api/admin/settings":
            # 必须排在 guest 的 /api/admin/ 通配之前，否则会被它截胡成 404
            try:
                self._send(200, json.dumps(settings_mod.ui(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
        elif path == "/api/admin/security":
            # 同样必须排在 guest 的 /api/admin/ 通配之前
            self._send(200, json.dumps(security_view(), ensure_ascii=False))
        elif guest_mod and path.startswith("/api/admin/"):
            code, obj = guest_mod.handle_get(path, q, gid, True, ip)
            self._send(code, json.dumps(obj, ensure_ascii=False))
        elif path == "/api/update/check":
            force = str((q.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
            try:
                obj = updater.check(force=force) if updater else {
                    "ok": False, "current": "?", "has_update": False,
                    "error": "版本检测模块未加载"}
            except Exception as e:
                obj = {"ok": False, "current": "?", "has_update": False,
                       "error": "%s: %s" % (type(e).__name__, e)}
            self._send(200, json.dumps(obj, ensure_ascii=False))
        elif path == "/api/status":
            try:
                self._send(200, json.dumps(collect(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif path == "/api/public":
            try:
                self._send(200, json.dumps(public_info(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
        elif path == "/favicon.ico":
            self._send(204, b"")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    MAX_BODY = 256 * 1024        # 单个请求体上限，防止拿 Content-Length 撑爆内存

    def _read_json(self):
        self._body_too_big = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < 0:
            n = 0
        if n > self.MAX_BODY:
            # 只读掉上限那么多，剩下的不读并断开连接（否则连接复用会错位）
            try:
                self.rfile.read(self.MAX_BODY)
            except Exception:
                pass
            self._body_too_big = True
            self.close_connection = True
            return {}
        try:
            raw = self.rfile.read(n) if n else b""
            obj = json.loads(raw.decode("utf-8", "ignore") or "{}")
            # 只接受 JSON 对象：数组/字符串/数字会让后面 body.get 直接 AttributeError
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _too_big(self):
        """请求体超限时回 413，返回 True 表示已响应。"""
        if not getattr(self, "_body_too_big", False):
            return False
        self._send(413, json.dumps({"ok": False, "msg": "请求体过大"}, ensure_ascii=False))
        return True

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._csrf_ok():
            self._send(403, json.dumps({"ok": False, "msg": "跨站请求已被拒绝"},
                                       ensure_ascii=False))
            return
        if path in ("/login", "/logout"):
            self._redirect("/")
            return
        ip, proxied = self._client()
        local = self._is_local()

        # ---- 公网第一道闸（fail-closed）：封禁 → 暂停 → 路径白名单 ----
        if not local:
            left = _ban_left(ip)
            if left:
                self._send(403, json.dumps({
                    "ok": False, "msg": "这个网络访问过于频繁，已被临时限制（%d 分钟后自动解除）"
                                        % max(1, left // 60)}, ensure_ascii=False))
                return
            if public_paused():
                self._maintenance(as_json=True)
                return
            if path not in PUBLIC_POST_EXACT:
                _probe_hit(ip, path)
                self._local_only(as_json=True)
                return

        if guest_mod:
            gid, new_gid = guest_mod.gid_from(self.headers.get("Cookie"))
        else:
            gid, new_gid = "", False

        # 公开：访客提交翻唱申请 / 白名单申请（不需密码，翻唱要等站长批准；
        # 白名单申请本身也要站长批，且申请侧限流比点歌更紧）
        if guest_mod and path in ("/api/guest/request", "/api/guest/wl_apply"):
            body = self._read_json()
            if self._too_big():
                return
            code, obj = guest_mod.handle_post(path, body, gid, ip,
                                              self.headers.get("CF-IPCountry"),
                                              self.headers.get("User-Agent"), False)
            hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
            self._send(code, json.dumps(obj, ensure_ascii=False), headers=hdr)
            return

        # 剩下的全是管理操作：只认本机直连
        if not self._local_only(as_json=True):
            return

        body = self._read_json()
        if self._too_big():
            return
        # 模型市场：下载并安装（必须排在 guest 的 /api/admin/ 通配之前，也不与它同前缀）
        if market and path.startswith("/api/market/"):
            code, obj = market.handle_post(path, body)
            self._send(code, json.dumps(obj, ensure_ascii=False))
            return
        # 模型导入：选文件 / 识别 / 安装（必须排在 guest 的 /api/admin/ 通配之前）
        if model_import and path.startswith("/api/admin/import_"):
            try:
                code, obj = model_import.handle_post(path, body)
            except Exception as e:
                code, obj = 500, {"ok": False, "msg": str(e)}
            self._send(code, json.dumps(obj, ensure_ascii=False))
            return
        # 高级设置：读 / 存 / 恢复默认（必须排在 guest 的 /api/admin/ 通配之前）
        if settings_mod and path == "/api/admin/settings":
            act = str(body.get("act") or "save").lower()
            try:
                if act == "reset":
                    obj = settings_mod.reset()
                else:
                    vals = body.get("values")
                    if not isinstance(vals, dict):
                        vals = {k: v for k, v in body.items() if k in settings_mod._BY_KEY}
                    obj = settings_mod.save(vals)
            except Exception as e:
                obj = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
            self._send(200 if obj.get("ok") else 400,
                       json.dumps(obj, ensure_ascii=False))
            return
        # 版本更新：下载最新 tag 的源码覆盖代码目录（不碰数据与模型）
        if path == "/api/update/apply":
            try:
                obj = updater.apply() if updater else {"ok": False, "msg": "版本检测模块未加载"}
            except Exception as e:
                obj = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
            self._send(200 if obj.get("ok") else 400, json.dumps(obj, ensure_ascii=False))
            return
        # 安全操作：暂停/恢复公网访问、解除误封（必须排在 guest 的 /api/admin/ 通配之前）
        if path == "/api/admin/security":
            act = str(body.get("act") or "").lower()
            if act in ("pause", "resume"):
                obj = set_public_paused(act == "pause", body.get("note") or "")
            elif act == "unban":
                ipx = str(body.get("ip") or "").strip()
                with _probe_lock:
                    n = 1 if _ban.pop(ipx, None) else 0
                    if ipx:
                        _probe.pop(ipx, None)
                obj = {"ok": bool(n), "msg": "已解除 %s 的限制" % ipx if n else "这个 IP 不在限制列表里"}
            else:
                obj = {"ok": False, "msg": "未知操作"}
            self._send(200 if obj.get("ok") else 400,
                       json.dumps(obj, ensure_ascii=False))
            return
        # 管理员：审批
        if guest_mod and path.startswith("/api/admin/"):
            code, obj = guest_mod.handle_post(path, body, gid, ip,
                                              self.headers.get("CF-IPCountry"),
                                              self.headers.get("User-Agent"), True)
            self._send(code, json.dumps(obj, ensure_ascii=False))
            return

        if not (path == "/api/action" or (cover_mod and path.startswith("/api/cover/"))):
            self._send(404, json.dumps({"error": "not found"}))
            return

        if path != "/api/action":
            try:
                code, obj = cover_mod.handle_post(path, body)
            except Exception as e:
                code, obj = 500, {"ok": False, "msg": str(e)}
            self._send(code, json.dumps(obj, ensure_ascii=False))
            return

        act, target = body.get("do") or body.get("action"), body.get("target")
        force = bool(body.get("force"))
        if act == "start":
            if target == "api" and not port_open(7865) and not force:
                self._send(200, json.dumps({
                    "ok": False,
                    "msg": "RVC WebUI(7865) 还没就绪，此时启动 3333 会拿不到模型列表。"
                           "请点「⚡ 一键开启服务」，它会先起 WebUI 等就绪再起后端。"}, ensure_ascii=False))
                return
            ok, msg = start(target)
        elif act == "start_all":
            ok, msg = start_all()
        elif act == "stop_all":
            ok, msg = stop_all()
        elif act == "shutdown":
            ok, msg = stop_all()
            msg = (msg + "；面板即将关闭，下次用请双击「启动监控面板.bat」") if ok else msg
            self._send(200, json.dumps({"ok": ok, "msg": msg, "shutdown": True},
                                       ensure_ascii=False))
            # 先把响应发完再退，否则浏览器拿不到消息
            threading.Timer(1.5, lambda: os._exit(0)).start()
            return
        elif act == "stop":
            ok, msg = stop(target)
        elif act == "restart":
            ok1, m1 = stop(target)
            time.sleep(3)
            ok2, m2 = start(target)
            ok, msg = ok2, "%s / %s" % (m1, m2)
        else:
            ok, msg = False, "未知操作: %s" % act
        self._send(200, json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RVC 控制台</title>
<style>
  :root{
    --bg:#0f1420; --card:#182031; --card2:#1e283c; --line:#2a3550;
    --txt:#e8eefc; --dim:#8d9bb8; --ok:#22c55e; --bad:#ef4444; --warn:#f59e0b; --acc:#5b9dff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    padding:16px;max-width:1080px;margin:0 auto}
  h1{font-size:19px;margin:0 0 4px;font-weight:650;letter-spacing:.3px}
  .sub{color:var(--dim);font-size:12px}
  .top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px;flex-wrap:wrap}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:12px}
  .card h2{font-size:13px;margin:0 0 12px;color:var(--dim);font-weight:600;letter-spacing:.6px;text-transform:uppercase}
  .hero{background:linear-gradient(135deg,#1d2b47,#182031);border-color:#31456b}
  .hero .url{font-family:ui-monospace,Consolas,monospace;font-size:17px;color:#8ec5ff;word-break:break-all;line-height:1.5}
  .hero .hint{font-size:12px;color:var(--dim);margin-top:8px;line-height:1.7}
  .btn{background:var(--acc);color:#04121f;border:0;border-radius:8px;padding:7px 13px;font-size:12.5px;
    font-weight:650;cursor:pointer}
  .btn:active{transform:translateY(1px)}
  .btn.ghost{background:transparent;color:var(--txt);border:1px solid var(--line);font-weight:500}
  .btn.red{background:#3a1d24;color:#ff9aa8;border:1px solid #5c2b36}
  .btn.green{background:#12301f;color:#7ef0a8;border:1px solid #23543a}
  .btn:disabled{opacity:.45;cursor:default}
  .btn.big{padding:11px 22px;font-size:14px;border-radius:10px;font-weight:700;letter-spacing:.3px}
  .ctrl{background:linear-gradient(135deg,#17233a,#182031);border-color:#2f4066}
  .ctrl .tip{font-size:12px;color:var(--dim);line-height:1.7;margin-top:9px}
  .ctrl .tip b{color:#cfe3ff;font-weight:600}
  .row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #212b40;flex-wrap:wrap}
  .row:last-child{border-bottom:0}
  .pname{flex:1;min-width:150px}
  .pname b{font-size:13.5px;font-weight:600}
  .pname span{display:block;font-size:11.5px;color:var(--dim);margin-top:2px}
  .tag{font-family:ui-monospace,monospace;font-size:11px;color:var(--dim);
    background:#101827;border:1px solid var(--line);border-radius:6px;padding:2px 7px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
  .kv{background:var(--card2);border-radius:10px;padding:10px 12px}
  .kv .k{font-size:11px;color:var(--dim);margin-bottom:4px}
  .kv .v{font-size:14px;font-weight:600}
  .bar{height:6px;background:#0e1524;border-radius:99px;overflow:hidden;margin-top:7px}
  .bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#3b82f6,#22d3ee)}
  .ip{display:flex;align-items:center;gap:9px;padding:7px 0;border-bottom:1px dashed #212b40;flex-wrap:wrap}
  .ip:last-child{border-bottom:0}
  .ip code{font-family:ui-monospace,monospace;font-size:13px;color:#cfe3ff}
  .badge{font-size:10.5px;padding:1.5px 7px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
  .badge.on{color:#7ef0a8;border-color:#23543a;background:#12301f}
  pre{background:#0c1220;border:1px solid var(--line);border-radius:10px;padding:11px;
    font-size:11.5px;line-height:1.65;color:#9fb3d1;max-height:220px;overflow:auto;margin:0;
    font-family:ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
  .flash{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#20304d;
    border:1px solid #3a5077;color:var(--txt);padding:10px 18px;border-radius:10px;font-size:13px;
    opacity:0;transition:.25s;pointer-events:none;z-index:9;max-width:88vw;text-align:center}
  .flash.show{opacity:1}
  .foot{color:#5d6b87;font-size:11px;text-align:center;margin:16px 0 6px;line-height:1.8}
  a{color:#8ec5ff}
</style>
</head>
<body>
<div class="top">
  <div>
    <h1>RVC 控制台</h1>
    <div class="sub" id="meta">加载中…</div>
  </div>
  <div style="text-align:right">
    <div class="sub"><span class="dot" id="live" style="background:#475569"></span><span id="livetxt">连接中</span></div>
    <div style="display:flex;gap:8px;margin-top:7px;justify-content:flex-end">
      <a class="btn ghost" href="/" style="text-decoration:none;display:inline-block">← 控制台</a>
      <a class="btn" href="/cover" style="text-decoration:none;display:inline-block">🎤 翻唱</a>
      <button class="btn ghost" onclick="load(true)">立即刷新</button>
    </div>
  </div>
</div>

<div class="card ctrl">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <button class="btn green big" id="btnStart" onclick="act('start_all')">⚡ 一键开启服务</button>
    <button class="btn red big" id="btnStop" onclick="act('shutdown')">⏹ 一键退出服务</button>
    <span class="sub" id="ctrlState">…</span>
  </div>
  <div class="tip">
    <b>开启</b>：WebUI(7865) → 后端(3333) → 网易云(3002)，全自动等就绪；已在跑的会跳过。
    <b>退出</b>：按 后端 → 网易云 → WebUI 的顺序停掉，并关闭本面板。<br>
    拔移动硬盘、玩游戏、想让显卡空出来时用「一键退出」；只想停服务但留着面板看状态，用下面那张卡片里的「全部停止」。
  </div>
</div>

<div class="card hero">
  <h2>手机插件填这两个地址</h2>
  <div class="sub" style="margin-bottom:4px">① RVC API Gradio 后端服务地址（以 / 结尾）</div>
  <div class="url" id="pluginUrl">—</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin:9px 0 14px">
    <button class="btn" onclick="copyText(DATA&&DATA.plugin_url)">复制</button>
    <button class="btn ghost" onclick="openText(DATA&&DATA.plugin_url)">打开</button>
  </div>
  <div class="sub" style="margin-bottom:4px">② 网易云音乐 Node.js API 地址（本地服务，不用 https）</div>
  <div class="url" id="ncmUrl" style="font-size:15px">—</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:9px">
    <button class="btn" onclick="copyText(DATA&&DATA.ncm_url)">复制</button>
    <button class="btn ghost" onclick="openText((DATA&&DATA.ncm_url)+'/search?keywords=%E5%8D%81%E5%B9%B4&limit=3')">测试搜索</button>
  </div>
  <div class="hint" id="pluginHint"></div>
</div>

<div class="card">
  <h2>端口与进程</h2>
  <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
    <button class="btn" onclick="act('start_all')">启动缺失的服务</button>
    <button class="btn ghost" onclick="act('stop_all')">仅停服务（面板留着）</button>
    <span class="sub">启动顺序：RVC WebUI(7865) 就绪 → 再起后端(3333)</span>
  </div>
  <div id="ports"></div>
</div>

<div class="card">
  <h2>本机网络地址</h2>
  <div id="ips"></div>
</div>

<div class="card">
  <h2>RVC 状态</h2>
  <div class="grid" id="rvcinfo"></div>
</div>

<div class="card">
  <h2>显卡</h2>
  <div class="grid" id="gpu"></div>
</div>

<div class="card">
  <h2>后端日志 <span class="tag" id="logfile"></span></h2>
  <pre id="log">—</pre>
</div>

<div class="foot">
  每 3 秒自动刷新 · 面板端口 8899 · 手机与电脑需在同一局域网（同一 Wi-Fi 或同一热点）<br>
  电脑换网络后 IP 会变，以本页顶部地址为准重新填插件配置
</div>
<div class="flash" id="flash"></div>

<script>
let DATA = null;

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function toast(msg){
  const el = document.getElementById('flash');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(window._ft); window._ft = setTimeout(()=>el.classList.remove('show'), 2600);
}

async function load(manual){
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    DATA = await r.json();
    render();
    document.getElementById('live').style.background = '#22c55e';
    document.getElementById('livetxt').textContent = '已连接';
  }catch(e){
    document.getElementById('live').style.background = '#ef4444';
    document.getElementById('livetxt').textContent = '面板离线';
  }
}

function render(){
  const d = DATA;
  document.getElementById('meta').textContent = d.host + ' · ' + d.time;

  // 顶部地址
  document.getElementById('pluginUrl').textContent = d.plugin_url || '未获取到局域网 IP';
  document.getElementById('ncmUrl').textContent = d.ncm_url || '未获取到局域网 IP';
  const reach = d.lan_reach;
  let hint = '把这个地址填进 AstrBot 插件「RVC API Gradio 后端服务地址」，必须以 / 结尾。';
  if (reach === true) hint += ' ✅ 局域网内已可访问 3333。';
  else if (reach === false) hint += ' ⚠️ 后端没在跑，或防火墙没放行 3333。';
  if (d.gateway) hint += ' 当前网关 ' + d.gateway + '（热点共享时它就是另一台设备）。';
  const ncm = (d.ports||[]).find(p=>p.key==='ncm');
  hint += ncm ? (ncm.open ? ' 本地网易云 API(:3002) 已启动。' : ' ⚠️ 本地网易云 API(:3002) 未启动，插件搜不到歌。') : '';
  document.getElementById('pluginHint').textContent = hint;

  // 端口
  document.getElementById('ports').innerHTML = d.ports.map(p=>{
    const color = p.open ? '#22c55e' : '#475569';
    const st = p.open ? '运行中' : '未启动';
    let btns = '';
    if(p.key === 'panel'){
      btns = '<span class="tag">当前页面</span>';
    }else{
      btns =
        `<button class="btn green" onclick="act('start','${p.key}')" ${p.open?'disabled':''}>启动</button>` +
        `<button class="btn red" onclick="act('stop','${p.key}')" ${p.open?'':'disabled'} style="margin-left:6px">停止</button>` +
        `<button class="btn ghost" onclick="act('restart','${p.key}')" style="margin-left:6px">重启</button>`;
    }
    return `<div class="row">
      <div class="pname"><b><span class="dot" style="background:${color}"></span>${esc(p.name)}</b>
        <span>${esc(p.desc)}</span></div>
      <span class="tag" style="color:${p.open?'#7ef0a8':'#8d9bb8'}">:${p.port} ${st}</span>
      <span class="tag">${p.pid?('PID '+p.pid+(p.proc?(' · '+esc(p.proc)):'')):'—'}</span>
      <div>${btns}</div>
    </div>`;
  }).join('');

  // 总控条状态
  const svc = (d.ports||[]).filter(p=>p.key!=='panel');
  const onCnt = svc.filter(p=>p.open).length;
  const ctrl = document.getElementById('ctrlState');
  if(ctrl){
    if(onCnt === svc.length) ctrl.innerHTML = '<span class="dot" style="background:#22c55e"></span>全部在线 '+onCnt+'/'+svc.length;
    else if(onCnt === 0) ctrl.innerHTML = '<span class="dot" style="background:#475569"></span>全部停止 0/'+svc.length;
    else ctrl.innerHTML = '<span class="dot" style="background:#f59e0b"></span>部分运行 '+onCnt+'/'+svc.length;
  }

  // IP
  document.getElementById('ips').innerHTML = (d.ips.length? d.ips.map(x=>`
    <div class="ip">
      <code>${esc(x.ip)}</code>
      ${x.default?'<span class="badge on">默认出口</span>':''}
      ${x.private?'<span class="badge">内网</span>':'<span class="badge">公网/其他</span>'}
      <span style="color:#5d6b87;font-size:11.5px">${esc(x.iface||'')}</span>
    </div>`).join('') : '<div class="sub">没有检测到可用内网地址</div>');

  // RVC
  const rv = d.rvc || {};
  const mlist = (rv.models && rv.models.length)
    ? rv.models.map(m=>`<span class="tag" style="color:#cfe3ff;color:#cfe3ff">${esc(m.name)} · ${m.mb}MB</span>`).join(' ')
    : '<span style="color:#ff9aa8">assets/weights 里没有 .pth</span>';
  document.getElementById('rvcinfo').innerHTML = `
    <div class="kv"><div class="k">翻唱后端 :3333</div><div class="v" style="color:${rv.online?'#7ef0a8':'#ff9aa8'}">
      ${rv.online?'正常':'未启动'}</div></div>
    <div class="kv"><div class="k">换声引擎 :7865</div><div class="v" style="color:${rv.webui?'#7ef0a8':'#ff9aa8'}">
      ${rv.webui?'正常':'未启动'}</div></div>
    <div class="kv"><div class="k">当前生效索引</div><div class="v" style="font-size:12.5px;font-weight:500;word-break:break-all">
      ${esc(rv.active_index || '（还没跑过转换）')}</div></div>
    <div class="kv" style="grid-column:1/-1"><div class="k">可用模型</div><div class="v" style="line-height:2">${mlist}</div>
      ${rv.note?`<div class="k" style="margin-top:7px;color:#f59e0b">⚠️ ${esc(rv.note)}</div>`:''}</div>
    <div class="kv" style="grid-column:1/-1"><div class="k">索引文件（.index）</div>
      <div class="v" style="font-size:12px;font-weight:500;word-break:break-all">${esc((rv.indices&&rv.indices.length)?rv.indices.join('  ·  '):'—')}</div></div>`;

  renderGpu();
  renderLog();
}

function renderGpu(){
  const g = DATA && DATA.gpu;
  const box = document.getElementById('gpu');
  if(!g){ box.innerHTML = '<div class="kv"><div class="k">显卡</div><div class="v" style="color:#ff9aa8">nvidia-smi 不可用</div></div>'; return; }
  const mp = Math.round(100*g.mem_used/Math.max(1,g.mem_total));
  box.innerHTML = `
    <div class="kv" style="grid-column:1/-1"><div class="k">型号</div><div class="v">${esc(g.name)}</div></div>
    <div class="kv"><div class="k">GPU 利用率</div><div class="v">${g.util}%</div>
      <div class="bar"><i style="width:${g.util}%"></i></div></div>
    <div class="kv"><div class="k">显存</div><div class="v">${g.mem_used} / ${g.mem_total} MB <span style="font-size:11px;color:#8d9bb8">(${mp}%)</span></div>
      <div class="bar"><i style="width:${mp}%"></i></div></div>
    <div class="kv"><div class="k">温度</div><div class="v" style="color:${g.temp>80?'#ff9aa8':'#e8eefc'}">${g.temp} °C</div></div>`;
}

function renderLog(){
  const l = (DATA && DATA.log) || {lines:[]};
  document.getElementById('logfile').textContent = l.file || '无日志';
  document.getElementById('log').textContent = (l.lines && l.lines.length) ? l.lines.join('\n') : '（暂无日志）';
}

async function act(doWhat, target){
  const label = {start:'启动',stop:'停止',restart:'重启',start_all:'全部启动',
                 stop_all:'全部停止',shutdown:'退出服务'}[doWhat];
  if(doWhat === 'shutdown'){
    if(!confirm('将停止 后端(3333) / 网易云(3002) / RVC WebUI(7865)，并关闭本面板。\n\n' +
                '正在跑的翻唱任务会被中断；关掉后要再用，请双击「启动监控面板.bat」。\n\n确定退出？')) return;
  }else if(doWhat === 'stop' || doWhat === 'restart' || doWhat === 'stop_all'){
    if(!confirm(`确定要${label}吗？`)) return;
  }

  const btnS = document.getElementById('btnStart');
  const btnE = document.getElementById('btnStop');
  if(doWhat === 'shutdown'){
    toast('正在停止服务…');
    if(btnE){ btnE.disabled = true; btnE.textContent = '⏹ 退出中…'; }
    if(btnS) btnS.disabled = true;
  }else{
    toast(`${label}中…${doWhat==='start_all'?'（WebUI 加载 + 后端就绪，最长 3 分钟，别关页面）':''}`);
    if(doWhat === 'start_all'){
      if(btnS){ btnS.disabled = true; btnS.textContent = '⚡ 启动中…'; }
      if(btnE) btnE.disabled = true;
    }
  }

  try{
    const r = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({do:doWhat, target})});
    const j = await r.json();
    toast(j.msg || (j.ok?'完成':'失败'));
    if(j.shutdown){
      clearInterval(window._timer);
      document.getElementById('livetxt').textContent = '面板已关闭';
      document.getElementById('live').style.background = '#ef4444';
      if(btnE) btnE.textContent = '⏹ 已退出';
      setTimeout(()=>{ document.body.style.transition='opacity .8s'; document.body.style.opacity='.4'; }, 900);
      return;
    }
  }catch(e){ toast('操作失败: '+e); }

  if(doWhat === 'start_all'){
    if(btnS){ btnS.disabled = false; btnS.textContent = '⚡ 一键开启服务'; }
    if(btnE) btnE.disabled = false;
  }
  setTimeout(()=>load(), 2000);
}

function copyText(v){
  if(!v) return toast('没有可复制的地址');
  navigator.clipboard?.writeText(v).then(()=>toast('已复制：'+v)).catch(()=>{
    const t=document.createElement('textarea'); t.value=v; document.body.appendChild(t);
    t.select(); document.execCommand('copy'); t.remove(); toast('已复制：'+v);
  });
}
function openText(u){ if(u) window.open(u,'_blank'); }

// 首次全量渲染，之后每 3 秒刷新
load();
window._timer = setInterval(()=>{
  fetch('/api/status',{cache:'no-store'}).then(r=>r.json()).then(j=>{
    DATA=j; render();
    document.getElementById('live').style.background='#22c55e';
    document.getElementById('livetxt').textContent='已连接';
  }).catch(()=>{
    document.getElementById('live').style.background='#ef4444';
    document.getElementById('livetxt').textContent='面板离线';
  });
}, 3000);
</script>
</body>
</html>
"""


def _read_page(name, fallback=""):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return fallback


# 控制台首页：本机直连 / 看到的就是这个，服务开关、公网地址、审批与队列都聚在一页
CONSOLE_PAGE = _read_page("console_page.html",
                          "<h1>控制台页面缺失</h1><p>dashboard/console_page.html 不见了</p>")
MARKET_PAGE = _read_page("market_page.html",
                         "<h1>模型市场页面缺失</h1><p>dashboard/market_page.html 不见了</p>")


def main():
    os.system("")  # 让 Windows 控制台支持 ANSI
    try:
        httpd = PanelServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print("端口 %d 启动失败（可能已被占用）: %s" % (PORT, e))
        input("按回车退出…")
        return
    ips = lan_ips()
    d = next((x["ip"] for x in ips if x.get("default")), ips[0]["ip"] if ips else "127.0.0.1")
    print("=" * 56)
    print("  RVC 控制台已启动")
    print("  控制台:     http://127.0.0.1:%d/       （管理只在本机可用）" % PORT)
    print("  监控面板:   http://127.0.0.1:%d/panel" % PORT)
    print("  自助翻唱:   http://127.0.0.1:%d/cover" % PORT)
    print("  访客页:     http://127.0.0.1:%d/guest     （公网地址打开也是这个）" % PORT)
    print("  局域网/手机: http://%s:%d  → 只能看到访客页，管理功能已关闭" % (d, PORT))
    print("  管理没有密码这层：只认插网线/本机那台电脑的 127.0.0.1")
    print("  （关闭此窗口即停止面板，后端不受影响）")
    print("=" * 56)
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d/" % PORT)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
