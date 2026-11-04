# -*- coding: utf-8 -*-
"""任务栏图标：菜单项装配与生命周期。"""
import os
import time
import webbrowser

from .core import *          # noqa: F401,F403
from . import core, services
from .core import _TRAY, _open_dir
from .services import start_all, stop_all


def _tray_shutdown():
    """托盘菜单的「停止服务并退出面板」。"""
    try:
        stop_all()
    except Exception:
        pass
    stop_tray()
    time.sleep(0.5)
    os._exit(0)


def tray_items():
    """托盘菜单。顺序按「最常点的放上面」，危险动作放最后并有提示。"""
    url = "http://127.0.0.1:%d/" % PORT
    return [
        ("打开控制台", lambda: webbrowser.open(url)),
        ("打开安装目录", lambda: _open_dir(STACK)),
        None,
        ("一键开启服务", start_all),
        ("停止全部服务（面板留着）", lambda: stop_all(include_tunnel=False)),
        None,
        ("停止服务并退出面板", _tray_shutdown),
    ]


def start_tray():
    if not tray_mod or not tray_mod.should_start():
        return None
    try:
        obj = tray_mod.start("RVC 翻唱站 · 左键呼出菜单 · 控制台 %s" % ("127.0.0.1:%d" % PORT),
                             tray_items(),
                             on_double=lambda: webbrowser.open("http://127.0.0.1:%d/" % PORT))
    except Exception as e:
        print("托盘图标启动失败（不影响网页控制台）: %s" % e)
        return None
    if obj.hwnd:
        _TRAY["obj"] = obj
        print("  任务栏图标: 已放置（左键单击呼出菜单，左键双击打开控制台）")
    else:
        print("  任务栏图标: 没放上（%s），网页控制台照常可用" % (obj.error or "未知原因"))
    return obj


def stop_tray():
    obj = _TRAY.get("obj")
    if obj:
        try:
            obj.stop()
        except Exception:
            pass
        _TRAY["obj"] = None

