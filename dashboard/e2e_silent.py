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



def page_bundle(html_name, js_name):
    """页面 + 它引用的脚本（v2.0.0 起控制台拆成多页，脚本在 assets/ 下）。"""
    a = open(os.path.join(HERE, html_name), encoding="utf-8").read()
    b = open(os.path.join(HERE, "assets", js_name), encoding="utf-8").read()
    return a + "\n" + b


def panel_src():
    """面板的全部源码。

    v2.0.0 起 panel.py 只剩启动装配，实现拆到了 panelkit/*.py；接线检查要把
    拆开的文件一起读，否则「代码搬走了」会被误判成「代码没了」。
    """
    def rd(p):
        return open(os.path.join(HERE, p), encoding="utf-8").read().replace("\r\n", "\n")
    parts = [rd("panel.py")]
    d = os.path.join(HERE, "panelkit")
    for n in sorted(os.listdir(d)):
        if n.endswith(".py"):
            parts.append(rd(os.path.join("panelkit", n)))
    return "\n".join(parts)


def test_wiring():
    print("\n== C. 接线与脚本 ==")
    rd = lambda p: open(os.path.join(HERE, p), encoding="utf-8").read().replace("\r\n", "\n")
    psrc = panel_src()
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

    html = page_bundle("console_page.html", "page_overview.js")
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


# ------------------------------------------- D. 无窗口启动 + 自助翻唱任务可见

def test_quiet_and_cover_jobs():
    print("\n== D. 无窗口启动 / 自助翻唱任务可见 ==")
    rd = lambda p: open(os.path.join(HERE, p), encoding="utf-8").read().replace("\r\n", "\n")

    psrc = panel_src()
    chk("服务用 pythonw.exe 启动", "def gui_exe" in psrc and 'cmd = [gui_exe(cfg["exe"])]' in psrc)
    chk("只在同目录真有 pythonw 时才换", "if os.path.isfile(w)" in psrc and 'n.lower() == "python.exe"' in psrc)
    chk("分离启动带 CREATE_NO_WINDOW", "_CREATE_NO_WINDOW = 0x08000000" in psrc
        and "flags = _DETACHED_PROCESS | _CREATE_NO_WINDOW" in psrc)
    chk("仍保留 DETACHED + 独立进程组", "_DETACHED_PROCESS = 0x00000008" in psrc
        and "_CREATE_NEW_PROCESS_GROUP = 0x00000200" in psrc)
    chk("STARTUPINFO 要求隐藏窗口", "STARTF_USESHOWWINDOW" in psrc and "SW_HIDE" in psrc)
    n_flags = psrc.count("creationflags=detach_flags(")
    chk("服务/兜底 bat/隧道都走同一套标志", n_flags >= 3, n_flags)
    chk("隧道也带隐藏 startupinfo", "startupinfo=hidden_startup()" in psrc)

    nsrc = rd(os.path.join("..", "ncmapi", "ncm_proxy.py"))
    import ast
    ntree = ast.parse(nsrc)
    sys_calls = [n for n in ast.walk(ntree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "system"]
    in_calls = [n for n in ast.walk(ntree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "input"]
    chk("ncm 不再用 os.system 开 ANSI", not sys_calls and "_enable_ansi" in nsrc, len(sys_calls))
    chk("ncm 的 ipconfig 不再开窗", "creationflags=_CREATE_NO_WINDOW" in nsrc)
    chk("ncm 仅剩一处 input 且包在 isatty 里", len(in_calls) == 1 and "os.isatty(0)" in nsrc,
        len(in_calls))

    # --- 快照逻辑：直接往 _jobs 里塞假任务，别真去做单 ---
    import cover
    with cover._lock:
        saved = dict(cover._jobs)
    now = time.time()

    def job(state, created, started=None, ended=None, **kw):
        j = {"state": state, "stage": kw.pop("stage", ""), "created": created,
             "started": started, "ended": ended, "file": kw.pop("file", None),
             "error": None, "title": kw.pop("title", ""), "model": kw.pop("model", "x.pth"),
             "key_shift": 0, "acapella": False}
        j.update(kw)
        return j

    try:
        with cover._lock:
            cover._jobs.clear()
            cover._jobs.update({
                "r1": job("running", now - 60, now - 30, stage="派发任务（下载 + 人声分离 + 推理）…",
                          title="正在做的一首", model="foo.pth", key_shift=2),
                "q1": job("queued", now - 50, stage="排队中", title="等 GPU 的一首", acapella=True),
                "d1": job("done", now - 300, now - 290, now - 260, stage="完成",
                          title="刚做完的一首", file="done.mp3"),
            })
            for i in range(7):     # 再塞 7 条完成，验证只留最近 5 条
                cover._jobs["old%d" % i] = job("done", now - 900 - i, now - 800 - i, now - 700 - i,
                                               title="旧单 %d" % i)
        snap = cover.jobs_snapshot()
    finally:
        with cover._lock:
            cover._jobs.clear()
            cover._jobs.update(saved)

    chk("分成 running / queued / recent", len(snap["running"]) == 1
        and len(snap["queued"]) == 1 and len(snap["recent"]) == 5,
        (len(snap["running"]), len(snap["queued"]), len(snap["recent"])))
    chk("running 带阶段与已用时", snap["running"][0]["stage"].startswith("派发")
        and snap["running"][0]["elapsed"] >= 25, snap["running"][0])
    chk("模型名去掉 .pth 后缀", snap["running"][0]["model"] == "foo", snap["running"][0]["model"])
    chk("total 只统计未完成的单", snap["total"] == 2, snap["total"])
    chk("recent 按完成时间倒序取前 5", snap["recent"][0]["title"] == "刚做完的一首",
        [r["title"] for r in snap["recent"]])
    chk("完成项带成品文件名", snap["recent"][0]["file"] == "done.mp3")
    chk("快照带并发上限与占用", snap["cap"] >= 1 and snap["used"] >= 0)
    with cover._lock:
        cover._jobs.clear()
    try:
        empty = cover.jobs_snapshot()
        chk("没有任务时 total=0 且三段为空", empty["total"] == 0 and not empty["running"]
            and not empty["queued"] and not empty["recent"])
    finally:
        with cover._lock:
            cover._jobs.update(saved)

    cs = rd("cover.py")
    chk("新增 /api/cover/jobs 接口", 'path == "/api/cover/jobs"' in cs and "jobs_snapshot()" in cs)

    html = page_bundle("console_tasks_page.html", "page_tasks.js")
    chk("控制台有「自助翻唱任务」卡片", 'id="cjobs"' in html and "自助翻唱任务" in html)
    chk("控制台会拉快照接口", "'/api/cover/jobs'" in html)
    chk("refresh 里接上了它", "loadQueue(); loadCoverJobs();" in html)
    chk("卡片能显示阶段/用时/试听", "j.stage" in html and "已用" in html and "/api/cover/file" in html)
    # v2.0.0 起路由搬到 routes_get.py，接收者是 h 而不是 self
    i_local = psrc.find('_local_only(as_json=path.startswith("/api/"))')
    i_cover = psrc.find('elif cover_mod and path.startswith("/api/cover/")')
    chk("自助翻唱接口在本机校验之后（外网拿不到）", 0 < i_local < i_cover, (i_local, i_cover))


def main():
    print("RVC 静默启动 / 任务栏图标 回归测试")
    test_launcher()
    test_tray()
    test_wiring()
    test_quiet_and_cover_jobs()
    print("\n== 结果 ==")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
