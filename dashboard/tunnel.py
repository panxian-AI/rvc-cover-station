# -*- coding: utf-8 -*-
"""内网穿透：把本机 8899 面板通过 Cloudflare 临时隧道暴露到公网。

用法（一般由 E:\\启动RVC-公网.bat 调用，不用手敲）：

    python tunnel.py              # 起隧道，窗口里显示公网地址
    python tunnel.py --stop       # 关掉在跑的隧道

为什么用它：cloudflared 临时隧道免注册、免登录，直接给一个
https://xxxx.trycloudflare.com 的地址，对方打开输密码即可用。
缺点：每次重启地址都会变；机器或隧道关掉链接就失效。

注意：cloudflared 是本地反代，服务端看到的来源 IP 恒为 127.0.0.1，
所以面板的密码网关靠 CF-Connecting-IP 判断真实来源（见 auth.py）。
"""
import os
import re
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from paths import DATA, STACK  # noqa: E402

CF = os.path.join(DATA, "tools", "cloudflared.exe")
URL_FILE = os.path.join(HERE, "公网链接.txt")
TARGET = "http://127.0.0.1:8899"
PATTERN = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")


def ensure_cloudflared():
    if os.path.isfile(CF):
        return True
    print("[X] 没找到 cloudflared: %s" % CF)
    print("    下载: https://github.com/cloudflare/cloudflared/releases/latest/download/"
          "cloudflared-windows-amd64.exe")
    print("    存成上面那个路径即可。")
    return False


def run():
    if not ensure_cloudflared():
        return 1
    cmd = [CF, "tunnel", "--url", TARGET, "--no-autoupdate", "--loglevel", "info"]
    print("正在建立隧道（约 5~15 秒）…")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, bufsize=1, text=True,
                         encoding="utf-8", errors="replace")
    shown = False
    try:
        for line in p.stdout:
            m = PATTERN.search(line or "")
            if m and not shown:
                url = m.group(0)
                shown = True
                try:
                    with open(URL_FILE, "w", encoding="utf-8") as f:
                        f.write(url + "\n")
                except OSError:
                    pass
                print("\n" + "=" * 60)
                print("  公网地址:  %s" % url)
                print("  （对方打开后输入面板启动窗口里显示的访问密码）")
                print("  关闭本窗口 = 断开公网访问，本机/局域网仍可用")
                print("=" * 60 + "\n")
            elif not shown and ("ERR" in (line or "") or "failed" in (line or "").lower()):
                print("  " + line.strip()[:160])
        p.wait()
    except KeyboardInterrupt:
        p.terminate()
        print("\n隧道已断开")
        return 0
    if not shown:
        print("[X] 隧道没拿到地址，检查网络或上面报错")
        return 1
    print("隧道进程结束（地址已失效）。重新双击启动即可换新地址。")
    return 0


def stop():
    """按镜像名杀掉 cloudflared（Windows）。"""
    subprocess.run(["taskkill", "/IM", "cloudflared.exe", "/F"],
                   capture_output=True)
    print("已尝试关闭 cloudflared 进程")
    return 0


if __name__ == "__main__":
    if "--stop" in sys.argv:
        sys.exit(stop())
    sys.exit(run())
