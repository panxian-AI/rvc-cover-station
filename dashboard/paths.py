# -*- coding: utf-8 -*-
"""整套 RVC 栈的目录推导（唯一真源）。

为什么要有它：以前 panel.py / cover.py 里把 "D:\\RVCSVC-API"、"D:\\RVC-WebUI"
写死，项目一搬到移动硬盘（甚至盘符从 E: 变成 F:）就全线失效。
现在全部按「本文件在哪」往上推，搬到哪里都能跑。

目录布局（移动硬盘根 = STACK）：
    STACK\\RVC-Data\\runtime\\python312\\   内置 Python 解释器（两个 venv 的 home）
    STACK\\RVC-Data\\RVC-WebUI\\      WebUI：.venv / assets（模型·索引）/ logs 都在内部
    STACK\\RVC-Data\\RVCSVC\\         本项目
        dashboard\\                  本文件所在
        RVCSVC-API\\                 翻唱后端 3333
        apienv\\                     后端 venv
        covers\\                     成品库
        ncmapi\\                     网易云代理 3002
"""
import os

DASH = os.path.dirname(os.path.realpath(__file__))       # <PROJ>\dashboard（解掉联接）
PROJ = os.path.dirname(DASH)                            # <STACK>\RVC-Data\RVCSVC
DATA = os.path.dirname(PROJ)                            # <STACK>\RVC-Data
STACK = os.path.dirname(DATA)                           # 移动硬盘根（E:\ 之类）

RVC_ROOT = os.environ.get("RVC_WEBUI_ROOT") or os.path.join(DATA, "RVC-WebUI")
API_DIR = os.path.join(PROJ, "RVCSVC-API")
NCM_DIR = os.path.join(PROJ, "ncmapi")
COVERS_DIR = os.path.join(PROJ, "covers")

PY_API = os.path.join(PROJ, "apienv", "Scripts", "python.exe")
PY_WEB = os.path.join(RVC_ROOT, ".venv", "Scripts", "python.exe")
PY_RUNTIME = os.path.join(DATA, "runtime", "python312", "python.exe")

API_BAT = os.path.join(API_DIR, "启动 rvcapi.bat")
WEBUI_BAT = os.path.join(RVC_ROOT, "启动训练界面.bat")
NCM_BAT = os.path.join(NCM_DIR, "启动网易云API.bat")


def describe():
    """给面板/排查用的一行摘要。"""
    return "STACK=%s | WEBUI=%s | API=%s" % (STACK, RVC_ROOT, API_DIR)
