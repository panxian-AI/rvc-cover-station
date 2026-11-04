# -*- coding: utf-8 -*-
"""面板基础层。

只依赖标准库与同目录的兄弟模块（paths / perf / tray / guest / cover / market ...），
**不 import panelkit 里的任何其它模块** —— 这一层在最底下，谁都可以依赖它。
"""
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

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(THIS_DIR)          # dashboard/
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PORT = int(os.environ.get("RVC_PANEL_PORT") or 8899)
API_BASE = "http://127.0.0.1:3333"
WEBUI_BASE = "http://127.0.0.1:7865"

# 路径全部由 paths.py 按「本文件位置」推导，不写死盘符（整栈可搬去移动硬盘）
from paths import (API_BAT, API_DIR, COVERS_DIR, DATA, NCM_BAT, NCM_DIR, PROJ,  # noqa: E402
                   PY_API, PY_WEB, RVC_ROOT, STACK, WEBUI_BAT)

# 设备自适应档位（线程数 / 预热策略），只读探测，纯 stdlib，不拖慢面板启动
try:
    import perf as perf_mod
except Exception as _e:      # 极端情况下没有 perf.py，退回全默认
    perf_mod = None
    print("⚠️ 性能档位模块未加载: %s" % _e)

# 任务栏通知区图标：静默启动后没有黑窗口，托盘就是用户唯一的常驻入口。
# 起不来（无桌面 / 远程会话限制）只打一行日志，绝不能拦住面板。
try:
    import tray as tray_mod
except Exception as _e:
    tray_mod = None
    print("⚠️ 托盘模块未加载: %s" % _e)

_TRAY = {"obj": None}


def tray_up():
    """托盘是否真的挂上了（控制台据此显示「托盘已就绪」）。

    hwnd 存在只说明窗口建好了；Shell_NotifyIconW 失败时图标并不在通知区，
    必须同时看 error。否则状态页报「已就绪」，用户却点不到入口。
    """
    obj = _TRAY.get("obj")
    return bool(obj and obj.hwnd and not getattr(obj, "error", ""))

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

# 模型管理：盘点已装声线 / 删除（本机 /models 页）
try:
    import models_mgr
except Exception as _e:
    models_mgr = None
    print("⚠️ 模型管理未加载（models_mgr.py 导入失败）: %s" % _e)

# 依赖版本核对：requirements.txt 锁了版本，这里比对机器上实际装的是不是那几个
try:
    import deps as deps_mod
except Exception as _e:
    deps_mod = None
    print("⚠️ 依赖核对未加载（deps.py 导入失败）: %s" % _e)

# 数据备份与迁移：导出 zip / 从 zip 恢复（换机器、重装、手改坏了都能救回来）
try:
    import dataio as dataio_mod
except Exception as _e:
    dataio_mod = None
    print("⚠️ 数据备份未加载（dataio.py 导入失败）: %s" % _e)

# 访问控制：管理功能**只认本机直连**（回环且不经反代），没有密码、没有远程管理。
# 公网隧道、局域网、带转发头的请求一律拒绝；访客只能看访客页。
MSG_LOCAL_ONLY = "管理功能只在这台电脑上可用：请在运行面板的电脑上打开 http://127.0.0.1:%d/" % PORT

# 安全事件日志：谁试图从远程碰管理接口（被扫/被爆破时能事后查）
SEC_LOG = os.path.join(HERE, "security.log")
_sec_lock = threading.Lock()
_sec_last = {"ip": None, "t": 0.0}


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



_PAGE_CACHE = {}


def _read_page(name, fallback=""):
    """读 dashboard 下的页面文件，按 mtime 缓存。

    以前只在 import 时读一次，于是改完 HTML 必须重启面板才能看到——
    实际踩过：新加的「模型市场」入口在旧进程里根本不存在，看起来像功能没做。
    现在改成每次请求比一下文件时间，改完刷新浏览器就生效，不用重开窗口。
    """
    p = os.path.join(HERE, name)          # 页面文件都在 dashboard/ 下，不在 panelkit/
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return fallback
    hit = _PAGE_CACHE.get(name)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return fallback
    _PAGE_CACHE[name] = (mt, txt)
    return txt


def _open_dir(path):
    """用资源管理器打开目录（不是控制台，不会弹黑窗）。"""
    try:
        os.startfile(path)
    except Exception:
        pass

