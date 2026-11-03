# -*- coding: utf-8 -*-
"""强制停止 RVC 全套服务 —— 面板已经关掉、或者「一键退出」没停干净时用。

    python stop_service.py            # 停后端(3333)/网易云(3002)/WebUI(7865)/公网隧道
    python stop_service.py --panel    # 连监控面板(8899)一起停

为什么单独一个脚本：面板本身可能已经退出，或者它自己就是占着端口停不掉的那个。
这时只能靠一个不依赖面板的东西去收摊。

两个关键点（都是实际踩过的坑）：
  1. 同一端口可能有**多个**进程在监听（Windows 允许端口复用），逐个杀，不能只杀一个；
  2. 必须 taskkill /T 杀进程树，gradio 会派生子进程，只杀父级会留孤儿继续占端口。

只做减法：不碰模型、不碰配置、不碰成品库。
"""
import re
import subprocess
import sys
import time

CREATE_NO_WINDOW = 0x08000000

# --dry-run：只列出会停哪些进程，一个都不杀。第一次用、或者想先看看到底是谁占着端口时用。
DRY = "--dry-run" in sys.argv

SERVICES = [("后端 RVCSVC-API", 3333), ("网易云搜索", 3002), ("RVC WebUI", 7865)]
PANEL_PORT = 8899


def sh(args):
    try:
        return subprocess.run(args, capture_output=True, timeout=10,
                              creationflags=CREATE_NO_WINDOW).stdout.decode("gbk", "ignore")
    except Exception:
        return ""


def listening_pids(port):
    """该端口上**所有**监听 PID（不是第一个）"""
    pids = []
    for line in sh(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-2] == "LISTENING" and parts[-1].isdigit():
            if parts[1].rsplit(":", 1)[-1] == str(port):
                pid = int(parts[-1])
                if pid not in pids:
                    pids.append(pid)
    return pids


def tunnel_pids():
    """cloudflared 只出站不监听端口，按端口找不到，只能按进程名找"""
    pids = []
    out = sh(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/FO", "CSV", "/NH"])
    for line in out.splitlines():
        m = re.match(r'"[^"]*","(\d+)"', line)
        if m:
            pids.append(int(m.group(1)))
    return pids


def proc_name(pid):
    out = sh(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    m = re.match(r'"([^"]+)"', out.strip()) if out.strip() else None
    return m.group(1) if m else "?"


def kill(pid):
    if DRY:
        return
    sh(["taskkill", "/F", "/T", "/PID", str(pid)])


def kill_port(port, label):
    total = []
    for _round in range(2):
        pids = listening_pids(port)
        if not pids:
            break
        for pid in pids:
            print("     结束 %s (PID %d)" % (proc_name(pid), pid))
            kill(pid)
            total.append(pid)
        if DRY:
            break
        time.sleep(0.4)
    if DRY:
        if total:
            print("[预览] %s 会被停掉（端口 %d）" % (label, port))
        else:
            print("[--] %s 未在运行" % label)
        return True
    left = listening_pids(port)
    if left:
        print("[X] %s（端口 %d）没停掉，仍在监听：PID %s"
              % (label, port, "/".join(map(str, left))))
        return False
    if total:
        print("[OK] %s 已停止，端口 %d 已释放" % (label, port))
    else:
        print("[--] %s 未在运行" % label)
    return True


def main():
    want_panel = "--panel" in sys.argv
    print("=" * 56)
    print("  停止 RVC 服务" + ("（预览模式：只列清单，不会真停）" if DRY else ""))
    print("=" * 56)

    ok = True
    for label, port in SERVICES:
        if not kill_port(port, label):
            ok = False

    tp = tunnel_pids()
    if tp:
        for pid in tp:
            print("     结束 cloudflared (PID %d)" % pid)
            kill(pid)
        if DRY:
            print("[预览] 公网隧道会一并关闭")
        else:
            time.sleep(0.4)
            left = tunnel_pids()
            if left:
                print("[X] 公网隧道没停掉：PID %s" % "/".join(map(str, left)))
                ok = False
            else:
                print("[OK] 公网隧道已关闭")
    else:
        print("[--] 公网隧道未在运行")

    if want_panel:
        if not kill_port(PANEL_PORT, "监控面板"):
            ok = False

    print("=" * 56)
    if ok:
        if want_panel:
            print("  已完全停止，端口都空出来了")
        else:
            print("  服务已停止（面板还在跑；连面板一起停：stop_service.py --panel）")
    else:
        print("  有东西没停掉，看上面带 [X] 的行")
        print("  仍不行就重启电脑，或用任务管理器结束残留的 python.exe")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
