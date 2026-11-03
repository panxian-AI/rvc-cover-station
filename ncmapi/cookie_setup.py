# -*- coding: utf-8 -*-
"""网易云 Cookie 设置窗口。

双击「设置网易云Cookie.bat」打开。把浏览器里复制的 Cookie 粘进来，
程序自动提取 MUSIC_U 存到 ncm_cookie.txt，并当场检测登录是否生效。

只依赖标准库 + tkinter（Python 自带），无额外安装。
"""
import json
import os
import re
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "ncm_cookie.txt")
VERIFY_URL = "https://music.163.com/api/nuser/account/get"

HELP_TEXT = """\
获取步骤（1 分钟）：
  1. 用浏览器打开  https://music.163.com  并登录你的账号
  2. 按 F12 打开开发者工具 → 顶部选「网络 / Network」
  3. 刷新页面，随便点一个请求 → 「标头 / Headers」→ 找到「Cookie:」
  4. 把整段 Cookie 值复制粘贴到下面的框里（只需要含 MUSIC_U 即可）
     —— 也可以只复制 MUSIC_U 那一段：MUSIC_U=xxxxxxxx
保存后重启「网易云API」窗口生效。Cookie 一般几个月才过期，过期了重新粘贴一次。
"""

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def extract(raw):
    """从用户粘贴的内容里提取出可用的 Cookie 串。

    兼容三种输入：整段浏览器 Cookie / MUSIC_U=xxx / 只有值。
    返回 (cookie 串, 提示信息)，cookie 为空表示没提取到 MUSIC_U。
    """
    raw = " ".join((raw or "").split())
    if not raw:
        return "", "请先粘贴内容"
    if "MUSIC_U=" in raw:
        m = re.search(r"MUSIC_U=([^;\s]+)", raw)
        if m and m.group(1):
            return raw, ""
        return "", "MUSIC_U 的值是空的，重新复制一下"
    # 只有值：当成长 token 接受
    if re.fullmatch(r"[A-Za-z0-9%]{32,}", raw):
        return "MUSIC_U=" + raw, ""
    return "", "里面没有找到 MUSIC_U —— 确认登录了网易云再复制"


def load_saved():
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
            return f.read()
    except Exception:
        return ""


def verify(cookie, cb):
    """检测 Cookie 是否已登录。结果通过 cb(昵称或错误提示) 回调。"""
    def run():
        try:
            req = urllib.request.Request(
                VERIFY_URL,
                headers={"Cookie": cookie,
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                         "Referer": "https://music.163.com"})
            with OPENER.open(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            nick = (data.get("profile") or {}).get("nickname")
            if nick:
                cb("✅ 登录生效，账号：%s" % nick)
            else:
                cb("⚠️ 网易云收到了请求但没认出登录（Cookie 失效或不完整），重新复制一次")
        except Exception as e:
            cb("❌ 检测失败：%s: %s（网络不通也会这样）" % (type(e).__name__, e))
    threading.Thread(target=run, daemon=True).start()


class Setup:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("网易云 Cookie 设置 — RVC 点歌台")
        self.root.geometry("640x520")
        self.root.minsize(560, 460)
        self._build()
        saved = load_saved()
        if "MUSIC_U=" in saved:
            c, _ = extract(saved)
            self.txt.insert("1.0", c)
            self._status("检测到已保存的 Cookie，可点「检测登录态」看看还有没有效", "#8aa")

    def _build(self):
        pad = {"padx": 14, "pady": 4}
        tk.Label(self.root, text="网易云 Cookie 设置", font=("微软雅黑", 13, "bold")).pack(anchor="w", **pad)
        t = tk.Text(self.root, height=8, wrap="word", relief="flat",
                    background="#f4f6fa", font=("微软雅黑", 9))
        t.insert("1.0", HELP_TEXT)
        t.config(state="disabled")
        t.pack(fill="x", padx=14, pady=(2, 8))

        tk.Label(self.root, text="把 Cookie 粘到这里：", font=("微软雅黑", 10)).pack(anchor="w", **pad)
        self.txt = tk.Text(self.root, height=5, wrap="char", font=("Consolas", 10))
        self.txt.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=14)
        ttk.Button(row, text="保存并检测", command=self._save).pack(side="left")
        ttk.Button(row, text="只检测登录态", command=self._check_only).pack(side="left", padx=8)
        ttk.Button(row, text="清除（退出登录）", command=self._clear).pack(side="right")

        self.status = tk.Label(self.root, text="", font=("微软雅黑", 10), wraplength=580,
                               justify="left", anchor="w")
        self.status.pack(fill="x", padx=14, pady=10)

    def _status(self, s, color="#333"):
        self.status.config(text=s, fg=color)
        self.root.update_idletasks()

    def _clear(self):
        if not messagebox.askyesno("确认", "删除已保存的 Cookie？\n（之后网易云 API 会退回匿名模式）"):
            return
        try:
            os.remove(COOKIE_FILE)
        except FileNotFoundError:
            pass
        self.txt.delete("1.0", "end")
        self._status("已清除。重启「网易云API」后回到匿名模式。", "#b60")

    def _check_only(self):
        c, msg = extract(self.txt.get("1.0", "end"))
        if not c:
            self._status(msg, "#c33")
            return
        self._status("正在检测…", "#888")
        verify(c, lambda s: self._status(s, "#282" if s.startswith("✅") else "#c33"))

    def _save(self):
        c, msg = extract(self.txt.get("1.0", "end"))
        if not c:
            self._status(msg, "#c33")
            return
        try:
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                f.write("# 由 设置网易云Cookie.bat 窗口保存，有 MUSIC_U 才算登录态\n")
                f.write(c + "\n")
        except OSError as e:
            self._status("保存失败：%s" % e, "#c33")
            return
        self._status("已保存到 %s，正在检测登录态…" % os.path.basename(COOKIE_FILE), "#888")
        verify(c, self._after_verify)

    def _after_verify(self, s):
        ok = s.startswith("✅")
        if ok:
            s += "\n重启「网易云API」窗口即可生效（VIP / 独家曲目就能搜到了）"
        self._status(s, "#282" if ok else "#c33")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Setup().run()
