# -*- coding: utf-8 -*-
"""静默启动 / 任务栏图标 回归测试（v1.4.0）

覆盖三块：
  A. launch_silent.py  —— 端口探测、日志轮转、GBK 日志、单实例互斥、失败不弹框
  B. tray.py           —— ICO 生成、图标复用、菜单构造、真挂上通知区再干净退出
  C. 接线与脚本        —— panel / 控制台页面 / tunnel / uvr5 / 启动RVC.bat

必须在真实安装盘跑（依赖 E:\\RVC-Data 的真实目录）：
    cd E:\\RVC-Data\\RVCSVC\\dashboard && python e2e_silent.py
"""
import os
import re
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = []
FAIL = []


def chk(name, ok, detail=""):
    if ok:
        PASS.append(name)
        print("  [OK] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s %s" % (name, ("→ " + str(detail)) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------- A. 启动器

def test_launcher():
    print("\n== A. 静默启动器 ==")
    import launch_silent as L

    api, pyw = L.pick_python()
    chk("找到面板用的解释器", os.path.isfile(api), api)
    chk("找到 pythonw.exe（无控制台的前提）", bool(pyw) and os.path.isfile(pyw), pyw)
    chk("解释器真的能跑", L.python_ok(api))
    chk("坏解释器判 False", L.python_ok(os.path.join(tempfile.gettempdir(), "nope.exe")) is False)

    p = free_port()
    chk("没人监听的端口判 False", L.panel_up(p, 0.4) is False)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    live = srv.getsockname()[1]
    try:
        chk("有人监听的端口判 True", L.panel_up(live, 2.0) is True)
    finally:
        srv.close()

    tmp = tempfile.mkdtemp()
    log = os.path.join(tmp, "启动.log")
    with open(log, "wb") as f:
        f.write(b"x" * 5000)
    chk("未超限不轮转", L.rotate_log(log, 10 ** 6) is False)
    chk("超限轮转成 .1", L.rotate_log(log, 100) is True
        and os.path.isfile(log + ".1") and not os.path.exists(log))

    with open(log, "w", encoding="utf-8") as f:
        f.write("第一行\n第二行\n第三行\n")
    chk("tail 取末尾 N 行", L.tail(log, 2).splitlines()[-1] == "第三行")
    L.log_line(log, "中文不影响")
    raw = open(log, "rb").read()
    chk("日志按 GBK 写（和 cmd 那半边一致）", "中文不影响".encode("gbk") in raw)

    env = L.child_env()
    chk("子进程不再自己开浏览器", env.get("RVC_NO_BROWSER") == "1")
    chk("子进程标记静默", env.get("RVC_PANEL_SILENT") == "1")

    os.environ["RVC_NO_BROWSER"] = "1"
    try:
        chk("禁浏览器时不真开", L.maybe_browser() is False)
    finally:
        del os.environ["RVC_NO_BROWSER"]

    os.environ["RVC_NO_MSGBOX"] = "1"
    try:
        chk("测试/无人值守不弹模态框", L.show_error("标题", "内容") is False)
    finally:
        del os.environ["RVC_NO_MSGBOX"]

    name = "Local\\RVC_E2E_Silent_%d" % os.getpid()
    h1, busy1 = L.acquire_mutex(name)
    h2, busy2 = L.acquire_mutex(name)
    chk("同名互斥锁第二次识别为已存在", busy1 is False and busy2 is True, (busy1, busy2))
    L.release_mutex(h1)
    L.release_mutex(h2)

    src = open(os.path.join(HERE, "launch_silent.py"), encoding="utf-8").read()
    chk("拉起面板用 DETACHED（关掉启动器也不带走面板）", "DETACHED_PROCESS" in src)
    chk("有 breakaway 被拒时的降级", "winerror" in src and "!= 5" in src)
    chk("失败路径都会弹框", src.count("show_error(") >= 5, src.count("show_error("))


# --------------------------------------------------------------- B. 托盘

def test_tray():
    print("\n== B. 任务栏图标 ==")
    import tray as T

    blob = T.ico_bytes()
    chk("ICO 文件头正确", blob[:4] == b"\x00\x00\x01\x00")
    chk("ICO 含 32/16 两个尺寸", blob[4:6] == b"\x02\x00")

    xor, mask = T.icon_frames(16)
    chk("16px 像素帧长度", len(xor) == 16 * 16 * 4, len(xor))
    chk("掩码按 4 字节对齐", len(mask) == 16 * 4, len(mask))
    xor32, _m = T.icon_frames(32)
    chk("32px 像素帧长度", len(xor32) == 32 * 32 * 4)

    tmp = tempfile.mkdtemp()
    ico = T.ensure_icon(os.path.join(tmp, "t.ico"))
    chk("图标写盘成功", bool(ico) and os.path.getsize(ico) > 200,
        os.path.getsize(ico) if ico else "none")
    m1 = os.path.getmtime(ico)
    T.ensure_icon(ico)
    chk("已有图标直接复用（不反复写盘）", os.path.getmtime(ico) == m1)

    old = os.environ.get("RVC_NO_TRAY")
    os.environ["RVC_NO_TRAY"] = "1"
    chk("RVC_NO_TRAY=1 时不起托盘", T.should_start() is False)
    os.environ.pop("RVC_NO_TRAY", None)
    chk("默认允许起托盘（Windows）", T.should_start() is True)
    if old is not None:
        os.environ["RVC_NO_TRAY"] = old

    hits = []
    tr = T.Tray("e2e 自检", [("第一项", lambda: hits.append(1)), None,
                             ("第二项", lambda: hits.append(2))], icon_path=ico)
    ok = tr.start(6)
    chk("图标挂上通知区", bool(ok and tr.hwnd), tr.error)
    if tr.hwnd:
        import ctypes
        hm = tr._menu()
        chk("菜单跳过分隔线、id 连续", sorted(tr._ids) == [1, 2], sorted(tr._ids))
        ctypes.windll.user32.DestroyMenu(hm)
        tr._safe_call(tr._ids[2])
        chk("菜单回调真的会被调用", hits == [2], hits)
        tr.stop()
        time.sleep(0.8)
        chk("退出后清理干净（不留幽灵图标）", tr.hwnd is None)
    else:
        for n in ("菜单跳过分隔线、id 连续", "菜单回调真的会被调用", "退出后清理干净（不留幽灵图标）"):
            chk(n, False, "托盘没起来，跳过不了就只能失败")


# --------------------------------------------------------------- C. 接线

def test_wiring():
    print("\n== C. 接线与脚本 ==")
    rd = lambda p: open(os.path.join(HERE, p), encoding="utf-8").read().replace("\r\n", "\n")
    psrc = rd("panel.py")
    chk("面板导入托盘模块", "import tray as tray_mod" in psrc)
    chk("面板启动时放图标", "def start_tray" in psrc and "start_tray()" in psrc)
    chk("托盘菜单有启停入口", "def tray_items" in psrc and "一键开启服务" in psrc
        and "停止服务并退出面板" in psrc)
    chk("托盘退出走 os._exit", "_tray_shutdown" in psrc and "os._exit(0)" in psrc)
    chk("面板退出时删图标", "def stop_tray" in psrc and "stop_tray()" in psrc)
    chk("/api/status 带静默/托盘状态", '"silent":' in psrc and '"tray": tray_up()' in psrc)
    chk("新增启动日志接口", '"/api/panel_log"' in psrc and "def panel_log_tail" in psrc)
    chk("ANSI 不再靠 os.system 触发", "SetConsoleMode" in psrc and "_enable_ansi" in psrc)
    chk("无控制台时不再 input() 卡死", "def _wait_enter" in psrc)
    chk("兜底启动 bat 也静默", 'os.startfile(cfg["bat"])' not in psrc)
    chk("静默模式不重复开浏览器", 'RVC_NO_BROWSER' in psrc)

    html = rd("console_page.html")
    chk("控制台有静默/托盘提示位", 'id="silentLine"' in html)
    chk("控制台能看启动日志", 'id="logPre"' in html and "/api/panel_log" in html)
    chk("控制台写了左键用法", "左键单击呼出菜单" in html)

    tsrc = rd("tunnel.py")
    chk("cloudflared 不弹窗", "creationflags=0x08000000" in tsrc)

    for rel in ("../RVCSVC-API/uvr5/vr.py", "../RVCSVC-API/uvr5/mdxnet.py"):
        s = rd(rel)
        chk("%s 里 ffmpeg 静默执行" % os.path.basename(rel),
            "def _quiet" in s and 'os.system("ffmpeg' not in s)

    # 启动脚本（运行盘在 dashboard 的上两级；仓库副本在上两级再进目录）
    cands = [os.path.join(HERE, "..", "..", "启动RVC.bat"),
             os.path.join(HERE, "..", "启动RVC.bat"),
             os.path.join(HERE, "..", "..", "..", "启动RVC.bat")]
    bat_path = next((p for p in cands if os.path.isfile(p)), "")
    chk("找到 启动RVC.bat", bool(bat_path), cands)
    if bat_path:
        raw = open(bat_path, "rb").read()
        txt = raw.decode("gbk", "replace")
        chk("bat 是 GBK（cmd 里不乱码）", "一键启动" in txt)
        chk("bat 全是 CRLF", raw.count(b"\n") == raw.count(b"\r\n") and raw.count(b"\r\n") > 50)
        chk("bat 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
        chk("默认走静默分支", 'if not exist "%LAUNCH%" goto :visible' in txt)
        chk("优先 apienv 的 pythonw", "apienv\\Scripts\\pythonw.exe" in txt)
        chk("找不到 pythonw 就回退可见窗口", 'if not exist "%PYW%" goto :visible' in txt)
        chk("静默分支立刻退出（窗口一闪即走）", 'start "" "%PYW%"' in txt and "exit /b" in txt)
        chk("留了 --console 排障口", '"--console"' in txt)
        chk("RVC_CONSOLE=1 也能看窗口", "RVC_CONSOLE" in txt)
        chk("没有裸 %i 循环变量（会直接闪退）", not re.search(r"(?<!%)%i\b", txt))


def main():
    print("RVC 静默启动 / 任务栏图标 回归测试")
    test_launcher()
    test_tray()
    test_wiring()
    print("\n== 结果 ==")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
