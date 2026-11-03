# -*- coding: utf-8 -*-
"""停止服务回归测试：验证「一键退出」真的把端口退干净。

覆盖的都是实际踩过的坑：
  1. 同一端口多个监听者 → 必须全部杀掉（以前 setdefault 只留一个，只杀得掉一个）
  2. 杀完端口必须真的释放
  3. 公网隧道（cloudflared 不监听端口）不能被漏掉
  4. 没停干净时「一键退出」不能假装退出——面板必须留着，否则用户再也点不到停止

用 8890/8891 临时端口起假监听进程，不碰正在跑的后端。
    python e2e_stop.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8894"
os.environ["RVC_NO_AUTOPUMP"] = "1"
os.environ.setdefault("RVC_GUEST_DB", os.path.join(HERE, "_e2e_stop_guest.json"))

import panel  # noqa: E402
import stop_service  # noqa: E402

# 必须用「真实解释器」起假监听：venv 里的 python.exe 是个 launcher，
# 它会再拉一个子进程跑代码，Popen 拿到的 PID 不是真正在监听的那个。
PY = getattr(sys, "_base_executable", None) or sys.executable
if not os.path.isfile(PY):
    PY = sys.executable
PA, PB = 8890, 8891
SPAWN = []
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [XX] ") + name + (("  -> " + str(extra)) if extra else ""))


def spawn_listener(port):
    """起一个只占端口的假服务。

    复刻真实场景：socketserver / panel 都会设 SO_REUSEADDR，
    所以两个进程能同时监听一个端口（这就是 8899 叠 4 个实例的成因）。
    """
    code = (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', %d))\n"
        "s.listen(50)\n"
        "time.sleep(180)\n" % port
    )
    p = subprocess.Popen([PY, "-c", code], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    SPAWN.append(p)
    return p


def wait_port(port, want_open, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if panel.port_open(port) == want_open:
            return True
        time.sleep(0.2)
    return False


def main():
    # 用临时端口表，绝不碰 3333/7865/3002 上真在跑的服务
    real_ports = dict(panel.SERVICE_PORTS)
    panel.SERVICE_PORTS = {"api": PA}
    real_order = list(panel.STOP_ORDER)
    panel.STOP_ORDER = [("api", PA)]

    try:
        print("\n== 1. 端口上多个监听者都要能看见 ==")
        p1 = spawn_listener(PA)
        wait_port(PA, True)
        pids = panel.pids_on_port(PA, fresh=True)
        chk("单个监听能取到 PID", pids == [p1.pid], pids)

        p2 = spawn_listener(PA)
        time.sleep(1.2)
        pids = panel.pids_on_port(PA, fresh=True)
        chk("同一端口两个监听者都能取到", len(pids) == 2, pids)
        chk("stop_service 与 panel 的解析结果一致",
            sorted(stop_service.listening_pids(PA)) == sorted(pids), stop_service.listening_pids(PA))

        print("\n== 2. stop() 两个都要杀掉，端口要释放 ==")
        ok, msg = panel.stop("api")
        chk("stop 返回成功", ok, msg)
        chk("端口已释放", wait_port(PA, False, 8))
        both_dead = (not panel.pid_alive(p1.pid)) and (not panel.pid_alive(p2.pid))
        chk("两个监听进程都已结束", both_dead,
            "p1=%s p2=%s" % (panel.pid_alive(p1.pid), panel.pid_alive(p2.pid)))
        chk("提示里带上被杀 PID", str(p1.pid) in msg and str(p2.pid) in msg, msg)

        print("\n== 3. 未运行的服务 ==")
        ok3, msg3 = panel.stop("api")
        chk("未运行返回失败但不崩", (not ok3) and "未在运行" in msg3, msg3)

        print("\n== 4. stop_all 抓「端口还在但 PID 已经变了」的情况 ==")
        p3 = spawn_listener(PA)
        wait_port(PA, True)
        real_tunnel = panel.stop_tunnel
        panel.stop_tunnel = lambda: (True, "测试跳过隧道")
        try:
            ok4, msg4 = panel.stop_all(wait=6)
        finally:
            panel.stop_tunnel = real_tunnel
        chk("stop_all 成功", ok4, msg4)
        chk("stop_all 后端口释放", wait_port(PA, False, 8))
        chk("stop_all 后进程已死", not panel.pid_alive(p3.pid))

        print("\n== 5. 公网隧道：不监听端口也要能找到并关掉 ==")
        chk("tunnel_pids 返回列表", isinstance(panel.tunnel_pids(), list))
        ok5, msg5 = panel.stop_tunnel()
        chk("隧道不在跑时如实返回（不报错）", ok5 and "未运行" in msg5, msg5)

        print("\n== 6. 没停干净时，一键退出必须不退出 ==")
        # 造一个停不掉的服务：stop_all 直接返回失败
        srv = panel.PanelServer(("127.0.0.1", 8894), panel.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.8)

        real_stop_all = panel.stop_all
        panel.stop_all = lambda *a, **k: (False, "测试：端口 8890 仍被 1234(python.exe) 占着")
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8894/api/action",
                data=json.dumps({"action": "shutdown"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=15) as r:
                d = json.loads(r.read().decode())
        finally:
            panel.stop_all = real_stop_all

        chk("响应 ok=False", d.get("ok") is False, d)
        chk("响应 shutdown=False（不让前端以为已退出）", d.get("shutdown") is False, d.get("shutdown"))
        chk("提示里给了兜底办法", "停止RVC.bat" in d.get("msg", ""), d.get("msg"))
        time.sleep(2.2)
        alive = True
        try:
            op.open("http://127.0.0.1:8894/api/status", timeout=5).read()
        except Exception:
            alive = False
        chk("面板进程还活着（没被 os._exit 掉）", alive)
        srv.shutdown()
        srv.server_close()

        print("\n== 7. 全部停干净时，才真的退出 ==")
        # 放子进程里跑：退出分支会 os._exit(0)，不能把测试进程带走
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import panel\n"
            "panel.stop_all = lambda *a, **k: (True, '测试：全部已停止')\n"
            "panel.PanelServer(('127.0.0.1', 8893), panel.Handler).serve_forever()\n" % HERE
        )
        env = dict(os.environ)
        env["RVC_PANEL_PORT"] = "8893"
        env["PYTHONIOENCODING"] = "utf-8"
        child = subprocess.Popen([PY, "-c", code], env=env, cwd=HERE,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ok_boot = wait_port(8893, True, 15)
            chk("待退出的面板起来了", ok_boot)
            req = urllib.request.Request(
                "http://127.0.0.1:8893/api/action",
                data=json.dumps({"action": "shutdown"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with op.open(req, timeout=15) as r:
                d2 = json.loads(r.read().decode())
            chk("响应 shutdown=True", d2.get("shutdown") is True, d2)
            code_out = child.wait(timeout=15)
            chk("面板进程真的退出了（退出码 0）", code_out == 0, code_out)
            chk("退出后端口 8893 已释放", wait_port(8893, False, 8))
        finally:
            if child.poll() is None:
                child.kill()

    finally:
        panel.SERVICE_PORTS = real_ports
        panel.STOP_ORDER = real_order
        for p in SPAWN:
            try:
                if p.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, creationflags=0x08000000)
            except Exception:
                pass
        print("\n" + "=" * 52)
        print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
        if FAIL:
            for f in FAIL:
                print("  - " + f)
            sys.exit(1)


if __name__ == "__main__":
    main()
