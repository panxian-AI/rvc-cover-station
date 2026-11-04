# -*- coding: utf-8 -*-
"""RVC 控制台面板的内部包。

分层（下层不 import 上层）：

    core       常量 / 路径 / 可选子模块 / 页面缓存 / HTTP 小工具
      ├─ security   公网白名单、探测封禁、安全日志
      ├─ sysinfo    端口 · 进程 · GPU · 网络 · 日志
      │    └─ services  启停、预检、隧道
      ├─ pages      多页面登记表 + 静态资源
      ├─ trayapp    任务栏图标
      └─ routes_get / routes_post   路由（末端 if 链）
           └─ server    PanelServer + Handler

panel.py 只剩启动装配（main），所有对外接口由它重新导出，老代码 import panel 照用。
"""
from . import core, pages, security, server, services, sysinfo, trayapp  # noqa: F401

__all__ = ["core", "pages", "security", "server", "services", "sysinfo", "trayapp"]
