# -*- coding: utf-8 -*-
"""一次性拉起整套服务（面板 → WebUI → 后端 → 网易云代理）。

为什么需要它：沙箱会在命令结束时回收整个进程树，所以常驻服务必须在
沙箱外启动。用法（需在沙箱外执行）：

    python boot_stack.py

若面板已在运行，则只调它的一键启动接口，不会重复开进程。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

PY = r"E:\RVC-Data\runtime\python312\python.exe"      # 兜底，下面会按路径推导覆盖
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import PY_API, PY_RUNTIME
    # 面板必须用 apienv 起：审批通过后要 import gradio_client 真正跑翻唱，
    # 裸 runtime 里没有这个包（会 ModuleNotFoundError）。
    PY = PY_API if os.path.isfile(PY_API) else PY_RUNTIME
except Exception:
    pass
PANEL = os.path.join(HERE, "panel.py")
LOG = os.path.join(HERE, "panel_run.log")
PORTS = {"webui": 7865, "api": 3333, "ncm": 3002, "panel": 8899}
DETACHED = 0x00000008 | 0x00000200


def port_open(port, host="127.0.0.1"):
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def http_json(path, payload=None, timeout=120):
    url = "http://127.0.0.1:8899" + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    proxies = {}  # 空字典 = 绕开系统代理，走直连（本机回环不需要代理）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def main():
    # 1) 面板
    if port_open(PORTS["panel"]):
        print("[1/2] 面板已在运行，跳过启动")
    else:
        print("[1/2] 启动面板 ...")
        print("      解释器: %s" % PY)
        out = open(LOG, "ab", buffering=0)
        subprocess.Popen([PY, PANEL], cwd=HERE, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT,
                         creationflags=DETACHED, close_fds=True)
        for _ in range(30):
            if port_open(PORTS["panel"]):
                break
            time.sleep(1)
        else:
            print("      面板 30 秒内没起来，看 panel_run.log")
            return 1
        print("      面板就绪: http://127.0.0.1:8899")

    # 2) 其余服务（走面板的启动逻辑，顺序它已经处理好）
    print("[2/2] 一键启动 WebUI / 后端 / 网易云代理 ...")
    print("      " + str(http_json("/api/action", {"do": "start_all"})))

    deadline = time.time() + 240
    pending = ["webui", "api", "ncm"]
    while time.time() < deadline and pending:
        pending = [k for k in pending if not port_open(PORTS[k])]
        if pending:
            time.sleep(3)
    print("\n最终端口状态:")
    for name, port in PORTS.items():
        print("  %-6s %-5d %s" % (name, port, "✅ 监听中" if port_open(port) else "❌ 未监听"))
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())
