# -*- coding: utf-8 -*-
"""服务启停：无窗口分离启动、预检、按端口停进程、公网隧道。"""
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time

from .core import *          # noqa: F401,F403
from . import core, sysinfo
from .sysinfo import (_cache, _lock, listening_map, ncm_preflight, pids_on_port,
                      proc_name, webui_preflight)


# ---------------- 启停 ----------------
# 记录本面板亲手拉起的子进程 PID。
# 为什么需要：后端刚启动时端口还没监听（约 20 秒），
# 这时点「一键退出」按端口找不到 PID，进程会被漏掉变成孤儿。
_SPAWNED = {}
_spawn_lock = threading.Lock()


# ---------------- 无窗口分离启动 ----------------
# 只用 DETACHED_PROCESS 是不够的：把 Windows Terminal 设成「默认终端应用」的
# 机器上（Win11 默认就是），实测三个服务仍会各冒一个终端窗口，标题就是解释器
# 路径，用户看到的就是「点一下开启服务，桌面多出三个黑框」。
# 所以再显式带上 CREATE_NO_WINDOW（不创建控制台），并用 STARTF_USESHOWWINDOW
# + SW_HIDE 兜一层：老式 conhost 上这一层是唯一生效的那一层。
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def hidden_startup():
    """让子进程以隐藏姿态启动（不经 STARTUPINFO 时，某些终端宿主仍会给它开窗）。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def detach_flags(breakaway=True):
    """无窗口 + 无控制台 + 独立进程组（+ 可选脱离 job）。"""
    flags = _DETACHED_PROCESS | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    if breakaway:
        flags |= _CREATE_BREAKAWAY_FROM_JOB
    return flags


def gui_exe(exe):
    """把 python.exe 换成同目录的 pythonw.exe。

    这条才是真正止住「开启服务弹出三个终端」的关键：python.exe 是控制台子系统，
    即使带 DETACHED_PROCESS + CREATE_NO_WINDOW，在把 Windows Terminal 设成默认
    终端的 Win11 上仍会被 WT 接管、开出一个标签页（实测三个服务 = 三个终端窗口）。
    pythonw.exe 是 GUI 子系统，进程创建时就不申请控制台，桌面绝不会多窗口。
    stdout/stderr 本来就已经重定向进各自的日志文件，所以输出一个字都不丢。
    """
    d, n = os.path.split(str(exe))
    if n.lower() == "python.exe":
        w = os.path.join(d, "pythonw.exe")
        if os.path.isfile(w):
            return w
    return exe


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
        env.update(perf_mod.env() if perf_mod else {})   # 线程数按物理核给，别让 BLAS 开满 32 个抢核心
        if perf_mod:
            env.update(perf_mod.child_hints(target))
        env.update(cfg["env"])
        try:
            # 日志目录可能还不存在（全新安装 RVC-WebUI\logs 是空的），
            # 直接 open("ab") 会 Errno 2 让启动整个失败 —— 先补目录。
            log_dir = os.path.dirname(cfg["log"])
            if log_dir and not os.path.isdir(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            log = open(cfg["log"], "ab", buffering=0)
            log.write(("\n\n===== %s 由控制台启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
            cmd = [gui_exe(cfg["exe"])] + cfg["args"]
            kwargs = dict(cwd=cfg["cwd"], env=env, stdin=subprocess.DEVNULL,
                          stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                          startupinfo=hidden_startup())
            try:
                # 优先彻底脱离当前任务对象，关掉面板后子进程也能活
                proc = subprocess.Popen(cmd, creationflags=detach_flags(), **kwargs)
            except OSError as e:
                # 面板本身处在不允许 breakaway 的 job 里时会 WinError 5，
                # 去掉 breakaway 只要 DETACHED_PROCESS 一样能起（子进程随 job 走）
                if getattr(e, "winerror", None) != 5:
                    raise
                proc = subprocess.Popen(cmd, creationflags=detach_flags(False), **kwargs)
                print("breakaway 被拒绝，已用 DETACHED_PROCESS 降级启动 %s" % target)
            with _spawn_lock:
                _SPAWNED[target] = proc.pid
            return True, "已启动 %s，约 30~60 秒后端口就绪" % target
        except Exception as e:
            return False, "启动失败: %s" % e
    # 兜底：直接跑 bat
    if os.path.isfile(cfg["bat"]):
        try:
            # 仍然要静默：这条路径只在解释器缺失（安装不完整）时才会走到，
            # 但一旦弹窗，用户就会看到「点了下服务，桌面多出几个黑窗口」。
            # stdin 接空设备是有意的 —— 这些 bat 结尾的 pause 会立刻返回，
            # 不会留下一个看不见的进程挂在那里等按键。
            subprocess.Popen(["cmd", "/c", cfg["bat"]], cwd=os.path.dirname(cfg["bat"]),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             startupinfo=hidden_startup(),
                             creationflags=detach_flags())
            return True, "已通过脚本启动 %s（若几十秒后端口仍关闭，请手动双击 %s）" % (
                target, os.path.basename(cfg["bat"]))
        except Exception as e:
            return False, "启动失败: %s" % e
    return False, "找不到解释器或启动脚本"




def start_all():
    """WebUI / 后端 / 网易云三路并行拉起，再等端口逐个就绪。

    v1.2.1 起后端启动不再依赖 WebUI 先就绪：3333 打开页面时会自动刷新
    模型列表（app.load），晚到的 WebUI 不影响它，并行能把「一键开启」
    的总耗时从「WebUI + 后端」压成「最慢的那一个」。
    已经在跑的服务会跳过，不会重复开局端口冲突。"""
    notes = []

    # 1) WebUI
    if port_open(7865):
        notes.append("WebUI 已在运行")
    else:
        okw, msgw = webui_preflight()
        if not okw:
            return False, msgw
        ok1, m1 = start("webui")
        if not ok1:
            return False, "WebUI 启动失败: %s" % m1
        notes.append("WebUI 启动中")

    # 2) 后端：与 WebUI 并行启动（后端页面打开时会自动拉模型列表，
    #    不再需要「先等 WebUI 就绪再起后端」的串行等待）
    if port_open(3333):
        notes.append("后端已在运行")
    else:
        ok2, m2 = start("api")
        if not ok2:
            return False, "后端启动失败: %s" % m2
        notes.append("后端启动中")

    # 3) 网易云搜索，同样并行；Cookie 没配也能起，但要明确告诉用户去配置
    ncm_pf = ncm_preflight()
    if port_open(3002):
        notes.append("网易云已在运行")
    else:
        ok_n, m_n = start("ncm")
        notes.append("网易云已启动" if ok_n else "网易云启动失败: %s" % m_n)
    if ncm_pf["installed"] and not ncm_pf["cookie"]:
        notes.append("网易云 Cookie 未配置：点歌搜索会失败，"
                     "请双击 RVCSVC\\ncmapi\\设置网易云Cookie.bat 配置后重启网易云")

    # 4) 等后端真正监听再返回（后端轻量，一般先就绪）
    if not port_open(3333):
        t0 = time.time()
        while time.time() - t0 < 90:
            if port_open(3333):
                break
            time.sleep(1.5)
        else:
            return False, "后端进程已启动，但 90 秒内 3333 没就绪，请到「后端日志」看报错"
        notes.append("后端已就绪")

    # 5) 等 WebUI 端口就绪
    if not port_open(7865):
        for _ in range(60):  # 最多等 120 秒
            if port_open(7865):
                break
            time.sleep(2)
        else:
            return False, "WebUI 120 秒内没起来，请到 RVC 窗口看报错"
        time.sleep(2)
        notes.append("WebUI 已就绪")

    return True, "；".join(notes)


# 停止顺序：先后端，再网易云，最后 WebUI
# （反过来 WebUI 先死，后端拿不到模型列表会刷一屏报错）
STOP_ORDER = [("api", 3333), ("ncm", 3002), ("webui", 7865)]

# 服务 → 端口。单独抽出来，测试可以直接替换成临时端口，
# 不用真的去停用户正在跑的后端。
SERVICE_PORTS = {"api": 3333, "webui": 7865, "ncm": 3002}


def _kill_tree(pid):
    """杀掉进程及其整棵子进程树。

    为什么必须 /T：WebUI 是 gradio，主进程会派生下层进程，只杀父级会留下
    孤儿继续占着 7865——这正是「停止了但端口还在监听」的来源之一。
    """
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=10, creationflags=0x08000000)


def tunnel_pids():
    """cloudflared 的 PID 列表。

    它只往外出站、不监听本地端口，所以按端口永远找不到它，只能按进程名找。
    """
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=8,
                             creationflags=0x08000000).stdout.decode("gbk", "ignore")
        pids = []
        for line in out.splitlines():
            m = re.match(r'"[^"]*","(\d+)"', line)
            if m:
                pids.append(int(m.group(1)))
        return pids
    except Exception:
        return []


def stop_tunnel():
    """关掉公网隧道。返回 (ok, msg)"""
    pids = tunnel_pids()
    if not pids:
        return True, "公网隧道未运行"
    for pid in pids:
        _kill_tree(pid)
    t0 = time.time()
    while time.time() - t0 < 5 and tunnel_pids():
        time.sleep(0.3)
    left = tunnel_pids()
    if left:
        return False, "公网隧道没停掉（PID %s）" % "/".join(map(str, left))
    return True, "已关闭公网隧道"


TUNNEL_LOG = os.path.join(HERE, "tunnel.log")
URL_FILE = os.path.join(HERE, "公网链接.txt")


def _cf_exe():
    """cloudflared 路径。RVC_CLOUDFLARED 环境变量可覆盖（测试注入假路径用）。"""
    return os.environ.get("RVC_CLOUDFLARED") or os.path.join(DATA, "tools", "cloudflared.exe")


def _latest_url():
    """公网链接.txt 里最后一个 http 行（tunnel.py 拿到地址时写入）。"""
    try:
        with open(URL_FILE, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return ""
    for line in reversed(txt.splitlines()):
        line = line.strip()
        if line.startswith("http"):
            return line
    return ""


def start_tunnel(timeout=25):
    """UI 一键开公网隧道：拉起 tunnel.py（它负责解析 cloudflared 输出、写地址文件），
    然后等公网链接.txt 出现**新**地址。返回 (ok, msg)。

    等价于以前的双击「启动RVC-公网.bat」第 3 步，但不用再开命令行窗口。
    """
    if tunnel_pids():
        return True, "公网隧道已在运行"
    cf = _cf_exe()
    if not os.path.isfile(cf):
        return False, ("没找到 cloudflared.exe：%s。到 cloudflare 的 GitHub releases 下载 "
                       "cloudflared-windows-amd64.exe 放进 tools\\ 目录后再开。" % cf)
    try:
        os.remove(URL_FILE)   # 抹掉上次地址，避免把旧的当成刚拿到的
    except OSError:
        pass
    try:
        log = open(TUNNEL_LOG, "ab", buffering=0)
        log.write(("\n===== %s 由控制台启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
        subprocess.Popen([PY_API, "-u", os.path.join(HERE, "tunnel.py")],
                         cwd=HERE, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                         startupinfo=hidden_startup(),
                         creationflags=detach_flags())
    except Exception as e:
        return False, "隧道启动失败: %s" % e
    t0 = time.time()
    while time.time() - t0 < timeout:
        url = _latest_url()
        if url:
            return True, "公网已开启：%s（发给朋友即可，对方打开是访客点歌页）" % url
        time.sleep(1)
    if tunnel_pids():
        return True, "隧道进程已起来，但 %d 秒内还没拿到地址（网络慢），过几秒刷新页面看状态" % timeout
    return False, "隧道没建立起来（可能网络不通），详情看 dashboard\\tunnel.log"


def stop_all(wait=10, include_tunnel=True):
    """停掉三个服务 + 公网隧道，并等端口真正释放。返回 (ok, msg)

    include_tunnel：以前这里不管隧道，用户点「一键退出」后面板死了、后端也停了，
    但 cloudflared 还在后台跑，公网地址一直挂着（虽然只会返 502），
    体感就是「没退干净」。现在一并关掉。
    """
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
        # stop() 内部已经复查过端口，这里再等一小会儿兜住 TIME_WAIT
        t0 = time.time()
        while time.time() - t0 < wait and port_open(port):
            time.sleep(0.3)
        if port_open(port):
            parts.append("%s 端口 %d 仍未释放" % (name, port))
            all_ok = False
    if include_tunnel:
        ok_t, m_t = stop_tunnel()
        if not ok_t:
            all_ok = False
        parts.append(m_t)
    return all_ok, "；".join(parts)


def stop(target):
    port = SERVICE_PORTS.get(target)
    if not port:
        return False, "未知服务: %s" % target

    pids = pids_on_port(port, fresh=True) if port_open(port) else []
    how = "端口 %d" % port
    if not pids:
        # 端口还没起来（刚启动 20 秒内点退出）——用自己拉起的 PID
        with _spawn_lock:
            sp = _SPAWNED.get(target)
        if sp and pid_alive(sp):
            pids = [sp]
            how = "子进程记录"
    if not pids:
        return False, "%s 未在运行，无需停止" % target

    killed = []
    # 两轮：第一轮杀掉当前所有监听者（可能不止一个），第二轮抓「杀的过程中
    # 又起来的」或者 gradio 那种父死子重启的情况
    for _round in range(2):
        for pid in [p for p in pids if pid_alive(p)]:
            try:
                _kill_tree(pid)
                killed.append(pid)
            except Exception:
                pass
        time.sleep(0.5)
        with _lock:
            _cache["netstat"] = (0, {})
        pids = pids_on_port(port, fresh=True) if port_open(port) else []
        if not pids:
            break
    with _spawn_lock:
        _SPAWNED.pop(target, None)

    if pids:
        names = ", ".join("%d(%s)" % (p, proc_name(p) or "?") for p in pids)
        return False, "停止 %s 没完全成功：端口 %d 仍被 %s 占着" % (target, port, names)
    if not killed:
        return True, "%s 已停止（据%s，进程此前已自行退出）" % (target, how)
    return True, "已停止 %s (PID %s，据%s)" % (target, "/".join(map(str, killed)), how)



# ---------------- 公网隧道信息 ----------------
def public_info():
    """读 tunnel.py 写下的公网地址，并判断 cloudflared 是否还在跑。"""
    url = _latest_url()
    alive = False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
                             capture_output=True, timeout=6).stdout or ""
        alive = "cloudflared" in out.decode("gbk", "ignore").lower()
    except Exception:
        pass
    return {"ok": True, "url": url, "alive": alive}

