#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RVC 状态监控面板 —— 启动入口。

v2.0.0 起这里只剩「装配」：HTTP 服务怎么起、托盘怎么挂、出错怎么办。
真正的实现全在 panelkit/ 包里（core / security / sysinfo / services /
pages / routes_get / routes_post / server / trayapp），按依赖从下往上分层。

为了不让历史代码与测试「import panel 就能用」这件事失效，下面把老名字全部
重新导出；模块上还装了一个转发器：`panel.SERVICE_PORTS = {...}` 这类**赋值**
会同步到真正持有它的子模块（否则只改了门面，跑的还是旧值）。
"""
import ctypes
import http.server          # noqa: F401  （老代码/测试用 panel.http.server.xxx）
import json                 # noqa: F401
import os
import re                   # noqa: F401
import socket               # noqa: F401
import subprocess           # noqa: F401
import sys
import threading
import time
import types
import urllib.error         # noqa: F401
import urllib.parse         # noqa: F401
import urllib.request       # noqa: F401
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from panelkit import core, pages, security, server, services, sysinfo, trayapp  # noqa: E402
from panelkit.core import (API_BASE, API_BAT, API_DIR, COVERS_DIR, DATA,  # noqa: E402,F401
                           HERE as _CORE_HERE, MSG_LOCAL_ONLY, NCM_BAT, NCM_DIR, PORT,
                           PROJ, PY_API, PY_WEB, RVC_ROOT, SEC_LOG, STACK, WATCH,
                           WEBUI_BASE, WEBUI_BAT, _PAGE_CACHE, _TRAY, _open_dir,
                           _read_page, cover_mod, dataio_mod, deps_mod, guest_mod, http_code,
                           http_json, market, model_import, models_mgr, perf_mod,
                           port_open, settings_mod, tray_mod, tray_up, updater)
from panelkit.security import (PROBE_BAN, PROBE_LIMIT, PROBE_WINDOW,  # noqa: E402,F401
                               PUBLIC_GET_EXACT, PUBLIC_GET_PREFIX, PUBLIC_POST_EXACT,
                               public_paused, public_state, sec_log, security_view,
                               set_public_paused)
from panelkit.sysinfo import (active_index_from_log, collect, gateway_ip, gpu_info,  # noqa: E402,F401
                              lan_ips, listening_map, log_tail, newest_api_log,
                              panel_log_tail, pids_on_port, proc_name, rvc_models)
from panelkit.services import (SERVICES, SERVICE_PORTS, STOP_ORDER, _cf_exe,  # noqa: E402,F401
                               _kill_tree, _latest_url, detach_flags, gui_exe,
                               hidden_startup, ncm_preflight, pid_alive, public_info,
                               start, start_all, start_tunnel, stop, stop_all,
                               stop_tunnel, tunnel_pids, webui_preflight)
from panelkit.server import MAX_CONN, Handler, PanelServer  # noqa: E402,F401
from panelkit.trayapp import _tray_shutdown, start_tray, stop_tray, tray_items  # noqa: E402,F401

# 赋值转发：门面上改的这些名字，真正生效的模块要一起改。
# 为什么不能只改一个：子模块之间用 `from .core import *` 拿常量，各自持有一份
# 引用；只把 core.RVC_ROOT 换掉，sysinfo 里那份还是旧值，预检就会去看老目录。
_OWNER = {}
for _name in ("RVC_ROOT", "NCM_DIR", "STACK", "PROJ", "API_DIR", "API_BAT", "NCM_BAT",
              "WEBUI_BAT", "COVERS_DIR", "DATA", "PY_API", "PY_WEB", "PORT",
              "guest_mod", "cover_mod", "market", "model_import", "models_mgr",
              "settings_mod", "dataio_mod", "updater", "perf_mod", "tray_mod",
              "WATCH"):
    _OWNER[_name] = core
for _name in ("SERVICE_PORTS", "STOP_ORDER", "SERVICES", "start", "stop", "start_all",
              "stop_all", "start_tunnel", "stop_tunnel", "tunnel_pids", "pid_alive",
              "ncm_preflight", "webui_preflight", "public_info"):
    _OWNER[_name] = services
for _name in ("PROBE_LIMIT", "PROBE_WINDOW", "PROBE_BAN", "PUBLIC_GET_EXACT",
              "PUBLIC_GET_PREFIX", "PUBLIC_POST_EXACT"):
    _OWNER[_name] = security
for _name in ("collect", "listening_map", "pids_on_port", "lan_ips", "log_tail",
              "panel_log_tail", "gpu_info", "rvc_models"):
    _OWNER[_name] = sysinfo
for _name in ("PAGES", "ROUTES"):
    _OWNER[_name] = pages
del _name

# 星号导入让每个子模块各留一份同名引用，赋值时要一起刷新
_PKG = (core, pages, security, sysinfo, services, trayapp, server,
        sys.modules.get("panelkit.routes_get"), sys.modules.get("panelkit.routes_post"))


class _Facade(types.ModuleType):
    """让 `panel.X = v` 真的改到持有 X 的那个子模块。"""

    def __setattr__(self, name, value):
        if name in _OWNER:
            for m in _PKG:
                if m is not None and hasattr(m, name):
                    setattr(m, name, value)
            setattr(_OWNER[name], name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Facade


def _enable_ansi():
    """让 Windows 控制台认识 ANSI 颜色。

    以前是 `os.system("")` —— 一个空命令，副作用是让 cmd 打开 VT 处理。
    但静默启动时面板**没有控制台**，那一下会凭空新开一个看到就消失的黑窗口
    （正是这一版要消灭的东西）。改成直接调 SetConsoleMode，没控制台就静默失败。
    """
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        for h in (-11, -12):                       # STD_OUTPUT / STD_ERROR
            k32.SetConsoleMode(k32.GetStdHandle(h), 7)
    except Exception:
        pass


def _wait_enter(prompt="按回车退出…"):
    """只在真控制台里等按键。静默启动时 stdin 是空设备，input() 会抛
    EOFError 把整个进程带成一段 traceback。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except Exception:
        return
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


def main():
    _enable_ansi()

    try:
        httpd = PanelServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        busy = getattr(e, "winerror", None) == 10048 or getattr(e, "errno", None) in (48, 98, 10048)
        if busy:
            # 多半是重复双击了启动脚本。不要静默退出——直接把已经在跑的那个打开，
            # 否则用户会以为「启动了但页面上没有新功能」，其实是打到了更早的实例。
            print("=" * 56)
            print("  面板已经在运行了（端口 %d 被占用）" % PORT)
            print("  不会再起第二个实例，正为你打开已在运行的面板…")
            print()
            print("  要让它加载最新的代码：")
            print("    1. 双击 停止RVC.bat（连面板一起清干净）")
            print("    2. 再双击一次 启动RVC.bat")
            print("=" * 56)
            if os.environ.get("RVC_NO_BROWSER") != "1":
                try:
                    webbrowser.open("http://127.0.0.1:%d/" % PORT)
                except Exception:
                    pass
            time.sleep(3)
            return 0
        print("端口 %d 启动失败（可能已被其它程序占用）: %s" % (PORT, e))
        _wait_enter()
        return 1
    ips = lan_ips()
    d = next((x["ip"] for x in ips if x.get("default")), ips[0]["ip"] if ips else "127.0.0.1")
    print("=" * 56)
    print("  RVC 控制台已启动")
    print("  控制台:     http://127.0.0.1:%d/       （管理只在本机可用）" % PORT)
    print("  任务队列:   http://127.0.0.1:%d/console/tasks" % PORT)
    print("  访客与白名单: http://127.0.0.1:%d/console/guest" % PORT)
    print("  成品库:     http://127.0.0.1:%d/cover     （翻唱页第 3 节）" % PORT)
    print("  设置/数据:  http://127.0.0.1:%d/console/settings" % PORT)
    print("  监控面板:   http://127.0.0.1:%d/panel" % PORT)
    print("  自助翻唱:   http://127.0.0.1:%d/cover" % PORT)
    print("  访客页:     http://127.0.0.1:%d/guest     （公网地址打开也是这个）" % PORT)
    print("  局域网/手机: http://%s:%d  → 只能看到访客页，管理功能已关闭" % (d, PORT))
    print("  管理没有密码这层：只认插网线/本机那台电脑的 127.0.0.1")
    print("  （静默启动时没有本窗口；要停面板双击 停止RVC.bat，或用任务栏图标）")
    print("=" * 56)
    try:
        start_tray()
    except Exception as e:
        print("  任务栏图标异常（不影响使用）: %s" % e)
    # 静默启动（RVC_NO_BROWSER=1）由启动器在端口确认就绪后开浏览器，
    # 这里再开一次就会多出一个重复标签页。
    if os.environ.get("RVC_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d/" % PORT)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        stop_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
