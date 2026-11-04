# -*- coding: utf-8 -*-
"""无窗口启动器 —— 双击 启动RVC.bat 之后真正干活的是它。

为什么要有这个文件
------------------
python.exe 是**控制台子系统**程序，双击 bat 让它在里面跑，桌面上就必然
挂着一个黑窗口：用户开完服务还得自己把它最小化，关了窗口面板又跟着走。
这不是「启动慢」，是看着难受。

pythonw.exe 是 GUI 子系统、天生没有控制台。所以 bat 只做三件事：找到安装
目录、找到解释器、`start` 一下本文件然后立刻退出（黑窗口一闪，不到 1 秒）。
面板由这里用 DETACHED_PROCESS 拉起，stdout 进日志文件。

失败不能静默
------------
窗口没了，报错也看不见 —— 这是静默化最容易踩的坑。所以这里所有失败路径
都弹一个**原生消息框**（MessageBoxW，不依赖控制台），把原因和下一步动作
写清楚，同时追加进 启动RVC.bat 传进来的 _启动日志.txt。

失败可复现
----------
面板自己的 stdout/stderr 落在 dashboard/panel_console.log（超过 2MB 轮转），
控制台页面「服务状态」卡片里有「查看启动日志」，不用再去找黑窗口。

参数（bat 调用，用户不用管）
--------------------------
    --log <path>     启动日志文件（就是安装目录的 _启动日志.txt）
    --console        不静默，直接前台跑面板（等价于用户点名要看窗口）
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
PANEL = os.path.join(HERE, "panel.py")
CONSOLE_LOG = os.path.join(HERE, "panel_console.log")

PORT = int(os.environ.get("RVC_PANEL_PORT") or 8899)
TIMEOUT = float(os.environ.get("RVC_SILENT_TIMEOUT") or 45)
MUTEX_NAME = "Local\\RVC_Panel_Launch_Silent"

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

LOG_LIMIT = 2 * 1024 * 1024        # 面板控制台日志轮转阈值


# ------------------------------------------------------------------ 小工具

def log_line(path, msg):
    """往启动日志追加一行。cmd 那边是 chcp 936 写的，这里也必须按 GBK 写，
    否则一个文件里两种编码，用户用记事本打开就是乱码。"""
    if not path:
        return
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(path, "ab") as f:
            f.write(line.encode("gbk", "replace"))
    except OSError:
        pass


def _panel_answers(port, host="127.0.0.1", wait=0.6):
    """这个端口上跑的是不是**我们的面板**（不是「随便什么程序」）。

    只看 TCP 能不能连会把「别的程序占着 8899」当成面板已就绪：启动器于是
    不起面板、不报错，直接把浏览器开到一个不是面板的地址，用户看到白页。
    这里真的发一条 GET /api/ping，认 JSON 里的 app 标识。
    """
    try:
        s = socket.create_connection((host, port), timeout=wait)
    except OSError:
        return False
    try:
        s.settimeout(wait)
        s.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                  b"Connection: close\r\n\r\n" % int(port))
        buf = b""
        while len(buf) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
        return b"rvc-panel" in buf
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def panel_up(port=None, timeout=1.0, host="127.0.0.1"):
    """面板是否已经就绪：端口通 + /api/ping 回的是本程序的标识。"""
    port = PORT if port is None else port
    deadline = time.time() + max(0.0, timeout)
    while True:
        if _panel_answers(port, host):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def rotate_log(path=None, limit=None):
    """日志超限就重命名成 .1（旧的 .1 直接丢）。返回是否发生了轮转。"""
    path = CONSOLE_LOG if path is None else path
    limit = LOG_LIMIT if limit is None else limit
    try:
        if os.path.getsize(path) <= limit:
            return False
    except OSError:
        return False
    try:
        old = path + ".1"
        if os.path.exists(old):
            os.remove(old)
        os.replace(path, old)
        return True
    except OSError:
        return False


def tail(path, lines=12, limit=4000):
    """取文件末尾若干行，给消息框用（不能太长，消息框装不下）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            raw = f.read()
    except OSError:
        return ""
    txt = raw.decode("utf-8", "replace")
    out = "\n".join(txt.splitlines()[-lines:])
    return out[-limit:] if limit and len(out) > limit else out


def show_error(title, text, kind="error"):
    """原生消息框。静默模式下这是唯一还能让用户看见错误的方式。

    RVC_NO_MSGBOX=1 时只写日志不弹框 —— 自动化测试/无人值守安装用，
    否则一个模态框会把整条流水线挂在那里等不到人点确定。
    """
    log_line(os.environ.get("RVC_START_LOG"), "[弹框] %s: %s" % (title, text.replace("\n", " | ")))
    if os.environ.get("RVC_NO_MSGBOX") == "1":
        return False
    try:
        import ctypes
        flags = 0x00000010 | 0x00040000 | 0x00010000   # ICONERROR|TOPMOST|SETFOREGROUND
        if kind == "warn":
            flags = 0x00000030 | 0x00040000 | 0x00010000  # ICONWARNING
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 启动提示

class Splash:
    """右下角一张小卡片，告诉用户「正在启动 / 已启动」。

    为什么需要：双击 启动RVC.bat 后黑窗一闪就没了，浏览器又要等十几秒才
    打开 —— 这段空窗期里，用户（尤其是帮别人装的机器）完全分不清是在启动
    还是根本没反应，于是又双击一次，叠出第二个 WebUI 各占一份显存。

    tkinter 不可用时（精简版解释器、无桌面会话）整体降级成 no-op：
    绝不能因为「提示窗起不来」把启动本身搞挂。
    """

    W, H = 360, 104

    def __init__(self, title="RVC 翻唱站", text="正在启动…"):
        self.ok = False
        self.root = None
        try:
            import tkinter as tk
            root = tk.Tk()
            root.overrideredirect(True)          # 无边框
            root.attributes("-topmost", True)    # 压在其他窗口上面
            try:
                root.attributes("-alpha", 0.96)
            except Exception:
                pass
            x = root.winfo_screenwidth() - self.W - 28
            y = root.winfo_screenheight() - self.H - 72
            root.geometry("%dx%d+%d+%d" % (self.W, self.H, x, y))
            root.configure(bg="#1d2027")

            box = tk.Frame(root, bg="#1d2027", highlightthickness=1,
                           highlightbackground="#3b4150")
            box.pack(fill="both", expand=True)
            tk.Label(box, text=title, bg="#1d2027", fg="#e8eaed", anchor="w",
                     font=("Microsoft YaHei UI", 11, "bold")).pack(
                fill="x", padx=16, pady=(16, 4))
            self.lbl = tk.Label(box, text=text, bg="#1d2027", fg="#9aa4b2",
                                anchor="w", justify="left",
                                wraplength=self.W - 32,
                                font=("Microsoft YaHei UI", 9))
            self.lbl.pack(fill="x", padx=16)
            self.root = root
            self.ok = True
            self.pump(0.05)
        except Exception:
            self.ok = False

    def set(self, text):
        """换一行状态文字（并立刻重绘）。"""
        if not self.ok:
            return
        try:
            self.lbl.config(text=str(text))
            self.root.update()
        except Exception:
            self.ok = False

    def pump(self, seconds=0.0):
        """在 seconds 秒内持续泵 tkinter 事件，让窗口不至于「未响应」。

        不能用 mainloop()：这个进程还要同时等端口、读日志，主线程被
        mainloop 占住就没法干活了。手动 update 是这类启动器最省事的做法。
        """
        if not self.ok:
            if seconds > 0:
                time.sleep(seconds)
            return
        end = time.time() + max(0.0, seconds)
        while True:
            try:
                self.root.update()
            except Exception:
                self.ok = False
                return
            if time.time() >= end:
                return
            time.sleep(0.05)

    def close(self):
        if not self.ok:
            return
        try:
            self.root.destroy()
        except Exception:
            pass
        self.ok = False


# ------------------------------------------------------------------ 单实例

def acquire_mutex(name=MUTEX_NAME):
    """拿启动互斥锁。返回 (handle, already_running)。

    「已经有人在启动」和「已经在跑」是两件事：前者要等，不能并行再拉一个
    面板（第二个会在绑 8899 时失败退出，用户看到的是「双击没反应」）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        h = k32.CreateMutexW(None, False, name)
        if not h:
            return None, False
        return h, (k32.GetLastError() == 183)      # ERROR_ALREADY_EXISTS
    except Exception:
        return None, False


def release_mutex(handle):
    if not handle:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


# ------------------------------------------------------------------ 解释器

def stack_dirs():
    """安装根目录（含 runtime\\python312 的那一层）。

    正常布局是 <DATA>\\RVCSVC\\dashboard，往上两级就够。但仓库副本、或者
    用户把 RVC-Data 多套了一层，都会让「两级」猜错 —— 所以再往上多试两级，
    和 启动RVC.bat 里 :try_stack 的四种摆法保持一致。
    """
    proj = os.path.dirname(HERE)               # <DATA>\RVCSVC
    data = os.path.dirname(proj)               # <DATA>
    probe = data
    for _ in range(3):
        if os.path.isfile(os.path.join(probe, "runtime", "python312", "python.exe")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return data


def pick_python():
    """返回 (面板要用的 python.exe, 有没有 pythonw.exe)。"""
    data = stack_dirs()
    api = os.path.join(data, "RVCSVC", "apienv", "Scripts", "python.exe")
    if not os.path.isfile(api):
        api = os.path.join(data, "runtime", "python312", "python.exe")
    pyw = os.path.join(data, "RVCSVC", "apienv", "Scripts", "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = os.path.join(data, "runtime", "python312", "pythonw.exe")
    return api, (pyw if os.path.isfile(pyw) else "")


def find_fix_paths():
    data = stack_dirs()
    for cand in (os.path.join(data, "runtime", "fix_paths.py"),
                 os.path.join(data, "RVCSVC", "setup", "fix_paths.py"),
                 os.path.join(data, "setup", "fix_paths.py")):
        if os.path.isfile(cand):
            return cand
    return ""


def python_ok(exe, timeout=25):
    """解释器真的能跑才算数：文件在但被杀毒删剩半截、安装中断都会这样。"""
    if not os.path.isfile(exe):
        return False
    try:
        p = subprocess.run([exe, "-c", "print(1)"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode == 0
    except Exception:
        return False


# ------------------------------------------------------------------ 真正启动

def child_env():
    """面板子进程的环境。RVC_NO_BROWSER=1 让面板别自己开浏览器 ——
    由这里在端口确认就绪后再开，避免出现两个标签页。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["RVC_NO_BROWSER"] = "1"
    env["RVC_PANEL_SILENT"] = "1"
    return env


def spawn_panel():
    """DETACHED + 日志文件。返回 Popen。"""
    api, _pyw = pick_python()
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    f = open(CONSOLE_LOG, "ab", buffering=0)
    f.write(("\n\n===== %s 静默启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
    kwargs = dict(cwd=HERE, env=child_env(), stdin=subprocess.DEVNULL,
                  stdout=f, stderr=subprocess.STDOUT, close_fds=True)
    try:
        return subprocess.Popen([api, "-u", "panel.py"], creationflags=flags, **kwargs)
    except OSError as e:
        # 面板自己也处在不允许 breakaway 的 job 里时 WinError 5；
        # 去掉 breakaway 仍然能起，只是子进程随当前 job 走。
        if getattr(e, "winerror", None) != 5:
            raise
        return subprocess.Popen([api, "-u", "panel.py"],
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                                **kwargs)


def open_browser(port=None):
    port = PORT if port is None else port
    try:
        import webbrowser
        webbrowser.open("http://127.0.0.1:%d/" % port)
        return True
    except Exception:
        return False


def maybe_browser(port=None):
    """尊重 RVC_NO_BROWSER=1：自动化测试和无人值守启动别真去弹浏览器。"""
    if os.environ.get("RVC_NO_BROWSER") == "1":
        return False
    return open_browser(port)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    start_log = ""
    if "--log" in argv:
        i = argv.index("--log")
        if i + 1 < len(argv):
            start_log = argv[i + 1]
    os.environ["RVC_START_LOG"] = start_log
    log_line(start_log, "静默启动器启动  PORT=%d  超时=%.0fs" % (PORT, TIMEOUT))

    if "--console" in argv:
        # 点名要看窗口：原样前台跑，把控制台留给用户
        api, _ = pick_python()
        return subprocess.call([api, "-u", "panel.py"], cwd=HERE)

    # 走到这里有 UI 了：先弹一张「正在启动」的卡片，别让用户对着桌面发呆。
    splash = Splash()
    try:
        return _start_with_splash(start_log, splash)
    finally:
        splash.close()


def _start_with_splash(start_log, splash):
    # 1) 面板已经在跑：直接开页面，不重复起第二个实例
    if panel_up(PORT, 0.8):
        log_line(start_log, "面板已在运行，开浏览器")
        splash.set("面板已经在运行，正在打开浏览器…")
        splash.pump(1.0)
        maybe_browser()
        return 0

    handle, busy = acquire_mutex()
    try:
        if busy:
            # 另一个启动器正在起 → 等它，别并行抢 8899
            log_line(start_log, "另一个启动器正在启动，等它就绪…")
            splash.set("另一个启动窗口正在启动，等它就绪…")
            ready = False
            end = time.time() + TIMEOUT
            while time.time() < end:
                if panel_up(PORT, 0.6):
                    ready = True
                    break
                splash.set("另一个启动窗口正在启动，等它就绪…（已等 %d 秒）"
                           % int(TIMEOUT - max(0.0, end - time.time())))
            if ready:
                splash.set("已就绪，正在打开浏览器…")
                splash.pump(1.0)
                maybe_browser()
                return 0
            splash.close()          # 消息框是模态的，先让开
            show_error("RVC 启动较慢",
                       "面板 %d 秒内没能就绪。\n\n"
                       "可能是第一次启动（要扫模型目录），再等一会儿刷新页面试试；\n"
                       "一直打不开就双击安装目录里的「停止RVC.bat」，然后再启动一次。\n\n"
                       "日志：%s" % (int(TIMEOUT), CONSOLE_LOG),
                       kind="warn")
            return 1

        # 2) 前置体检：文件在 ≠ 能用
        if not os.path.isfile(PANEL):
            splash.close()
            show_error("RVC 启动失败",
                       "找不到控制台程序：\n%s\n\n"
                       "说明安装不完整。请重新双击「一键安装.bat」补齐文件"
                       "（已装好的模型不会重下）。" % PANEL)
            log_line(start_log, "[X] 缺 panel.py")
            return 2

        api, _pyw = pick_python()
        if not python_ok(api):
            fix = find_fix_paths()
            if fix:
                log_line(start_log, "解释器不能用，先跑路径校正: %s" % fix)
                try:
                    subprocess.run([api if os.path.isfile(api) else sys.executable, fix],
                                   cwd=os.path.dirname(fix), timeout=180,
                                   creationflags=CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        if not python_ok(api):
            splash.close()
            show_error("RVC 启动失败",
                       "内置 Python 运行不起来：\n%s\n\n"
                       "常见原因：\n"
                       "  1. 杀毒软件 / Windows Defender 把文件隔离了\n"
                       "  2. 安装没完成（网络中断，只下到半个文件）\n"
                       "  3. 文件夹被网盘同步损坏\n\n"
                       "解决：重跑一次「一键安装.bat」，装完把安装目录加进杀毒白名单。\n\n"
                       "日志：%s" % (api, start_log or CONSOLE_LOG))
            log_line(start_log, "[X] python 不可执行: %s" % api)
            return 3

        # 3) 起面板
        rotate_log()
        try:
            proc = spawn_panel()
        except Exception as e:
            splash.close()
            show_error("RVC 启动失败", "面板进程没能创建：%s\n\n日志：%s" % (e, CONSOLE_LOG))
            log_line(start_log, "[X] Popen 失败: %s" % e)
            return 4

        deadline = time.time() + TIMEOUT
        splash.set("正在启动面板…")
        while time.time() < deadline:
            if panel_up(PORT, 0.6):
                log_line(start_log, "面板就绪 pid=%s  http://127.0.0.1:%d/" % (proc.pid, PORT))
                splash.set("已启动，正在打开浏览器…\nhttp://127.0.0.1:%d/" % PORT)
                splash.pump(1.2)
                maybe_browser()
                return 0
            if proc.poll() is not None:
                # 提前死了：多半是 8899 被别的程序占着，或依赖缺失
                log_line(start_log, "[X] 面板提前退出 rc=%s" % proc.returncode)
                splash.close()      # 消息框是模态的，先让开
                show_error("RVC 面板启动失败",
                           "面板进程刚起来就退出了（退出码 %s）。\n\n"
                           "最常见是 8899 端口被别的程序占着：\n"
                           "  1. 双击安装目录里的「停止RVC.bat」（会连面板一起清掉）\n"
                           "  2. 再双击「启动RVC.bat」\n\n"
                           "如果还不行，重跑一次「一键安装.bat」。\n\n"
                           "面板日志：\n%s" % (proc.returncode, tail(CONSOLE_LOG, 10)),
                           kind="warn")
                return 5
            splash.set("正在启动面板…（首次启动要扫模型目录，约 10~30 秒）\n已等待 %d 秒"
                       % int(TIMEOUT - max(0.0, deadline - time.time())))
            time.sleep(0.3)

        log_line(start_log, "[X] %.0f 秒内端口未就绪" % TIMEOUT)
        splash.close()
        show_error("RVC 启动较慢",
                   "面板 %d 秒内还没就绪。\n\n"
                   "第一次启动要扫模型目录，机械硬盘上可能久一点：\n"
                   "再等 10 秒刷新 http://127.0.0.1:%d/ 试试。\n\n"
                   "一直打不开就双击「停止RVC.bat」清干净再来。\n\n"
                   "面板日志：\n%s" % (int(TIMEOUT), PORT, tail(CONSOLE_LOG, 10)),
                   kind="warn")
        return 6
    finally:
        release_mutex(handle)


if __name__ == "__main__":
    sys.exit(main())
