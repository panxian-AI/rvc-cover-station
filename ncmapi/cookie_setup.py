# -*- coding: utf-8 -*-
"""网易云登录窗口：扫码登录（推荐）+ 粘贴 Cookie（备选）。

双击「设置网易云Cookie.bat」打开。

扫码登录走网易云官方的二维码接口（和手机 App「扫一扫」登录网页版是同一套）：
  1. POST /api/login/qrcode/unikey  申请一个 unikey
  2. 把 https://music.163.com/login?codekey=<unikey> 画成本地二维码
  3. 每 2 秒 POST /api/login/qrcode/client/login 询问状态
     801 等待扫码 / 802 已扫码待确认 / 803 成功（响应里带登录 Cookie）/ 800 过期
  4. 803 时把 MUSIC_U 等 Cookie 写进 ncm_cookie.txt

只依赖标准库 + tkinter；二维码自己画（见 _qr.py），不装任何第三方包。
"""
import http.cookiejar
import json
import os
import re
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import messagebox, ttk

try:
    from . import _qr                      # 作为包导入时
except ImportError:
    import _qr                             # 直接跑脚本时

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "ncm_cookie.txt")
QR_IMG = os.path.join(HERE, "_qr_tmp.ppm")

BASE = "https://music.163.com"
VERIFY_URL = BASE + "/api/nuser/account/get"
QR_KEY_URL = BASE + "/api/login/qrcode/unikey"
QR_POLL_URL = BASE + "/api/login/qrcode/client/login"
QR_TEXT_URL = BASE + "/login?codekey=%s"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

HELP_TEXT = """\
扫码登录（推荐，10 秒）：
  点下面的「扫码登录」，用手机上的网易云音乐 App 扫窗口里的二维码
  → 手机上点「确认登录」即可。二维码 5 分钟内有效，过期了点「换一张」。

为什么还需要粘贴 Cookie：扫码接口偶尔会被风控拦（提示「二维码生成失败」），
这时可以按下面的老办法手工粘贴，效果完全一样。

手工粘贴（备选）：
  1. 用浏览器打开 https://music.163.com 并登录
  2. F12 → 「网络 / Network」→ 刷新页面 → 点任意请求 → Headers → Cookie
  3. 把整段复制到下面的框里（含 MUSIC_U 即可）
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


def save_cookie(cookie):
    """写盘。注释行以 # 开头，ncm_proxy 读的时候会跳过。"""
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write("# 由 设置网易云Cookie.bat 窗口保存，有 MUSIC_U 才算登录态\n")
        f.write(cookie.strip() + "\n")


class NcmLogin(object):
    """扫码登录的网络部分（与界面无关，方便单独测）。

    共用同一个 cookie jar：unikey 申请时网易云会种 NMTID，后续轮询要带上，
    否则个别账号会被判成不同会话。
    """

    def __init__(self, opener=None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar))

    def _post(self, url, fields, cookie="", timeout=12):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": UA,
            "Referer": BASE + "/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie or "os=pc; appver=2.9.7",
        })
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))

    def new_key(self):
        """申请 unikey。失败时抛异常，由界面显示成「二维码生成失败」。"""
        d = self._post(QR_KEY_URL, {"type": 1})
        key = d.get("unikey") or ""
        if not key:
            raise RuntimeError("网易云没返回 unikey（code=%s）" % d.get("code"))
        return key

    def poll(self, key):
        """返回 (code, cookie)。

        code: 800 过期 / 801 等待扫码 / 802 已扫码待确认 / 803 成功
        cookie: 仅 803 时有值
        """
        d = self._post(QR_POLL_URL, {"key": key, "type": 1})
        code = d.get("code")
        cookie = ""
        if code == 803:
            cookie = d.get("cookie") or self._jar_cookie()
        return code, cookie

    def _jar_cookie(self):
        """从 jar 里拼 Cookie：只取 music.163.com 的，按名字排序保证稳定。"""
        items = []
        for c in self.jar:
            if c.value and (not c.domain or "163.com" in c.domain):
                items.append((c.name, c.value))
        items.sort()
        return "; ".join("%s=%s" % kv for kv in items)

    def nickname(self, cookie):
        """用给定 Cookie 查昵称；未登录返回空串。"""
        req = urllib.request.Request(VERIFY_URL, headers={
            "Cookie": cookie, "User-Agent": UA, "Referer": BASE})
        with OPENER.open(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return ((d.get("profile") or {}).get("nickname") or "")


def verify(cookie, cb):
    """检测 Cookie 是否已登录。结果通过 cb(昵称或错误提示) 回调。"""
    def run():
        try:
            req = urllib.request.Request(
                VERIFY_URL,
                headers={"Cookie": cookie,
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                         "Referer": BASE})
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


class QrWindow(object):
    """二维码窗口：显示码、轮询状态、成功后回调。"""

    POLL_MS = 2000

    def __init__(self, master, login, on_success):
        self.login = login
        self.on_success = on_success
        self.key = ""
        self.alive = True
        self.busy = False

        self.top = tk.Toplevel(master)
        self.top.title("扫码登录网易云")
        self.top.geometry("360x470")
        self.top.resizable(False, False)
        self.top.transient(master)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        tk.Label(self.top, text="用手机网易云音乐 App 扫码",
                 font=("微软雅黑", 12, "bold")).pack(pady=(14, 2))
        tk.Label(self.top, text="App 首页左上角「☰」→ 扫一扫",
                 font=("微软雅黑", 9), fg="#666").pack()
        self.pic = tk.Label(self.top, background="#fff", width=290, height=290)
        self.pic.pack(pady=10)
        self.status = tk.Label(self.top, text="正在生成二维码…", font=("微软雅黑", 10),
                               fg="#333", wraplength=320, justify="center")
        self.status.pack(fill="x")
        self.refresh_btn = ttk.Button(self.top, text="换一张二维码", command=self.refresh)
        self.refresh_btn.pack(pady=10)
        self.refresh()

    # ------------------------------------------------------------ 二维码
    def refresh(self):
        self._set("正在生成二维码…", "#333")
        self.top.update_idletasks()
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            key = self.login.new_key()
        except Exception as e:
            self._ui(lambda: self._fail("二维码生成失败：%s\n可以按下面的老办法粘贴 Cookie" % e))
            return
        self.key = key
        try:
            mat, _, _ = _qr.encode(QR_TEXT_URL % key)
            ppm = _qr.to_ppm(mat, scale=6, border=2)
        except Exception as e:
            self._ui(lambda: self._fail("二维码绘制失败：%s" % e))
            return
        self._ui(lambda: self._show(ppm))

    def _show(self, ppm):
        if not self.alive:
            return
        try:
            # Tk 对二进制 data= 的解析不可靠，落一个临时文件再加载（PPM 是 Tk 原生格式）
            with open(QR_IMG, "wb") as f:
                f.write(ppm)
            self.photo = tk.PhotoImage(file=QR_IMG)
        except Exception as e:
            self._fail("二维码显示失败：%s" % e)
            return
        self.pic.config(image=self.photo, width=self.photo.width(),
                        height=self.photo.height())
        self._set("等待扫码…（二维码 5 分钟内有效）", "#333")
        self.refresh_btn.config(state="normal")
        self._schedule()

    def _fail(self, text):
        if not self.alive:
            return
        self.pic.config(image="", width=290, height=290)
        self._set(text, "#c33")
        self.refresh_btn.config(state="normal")

    # ------------------------------------------------------------ 轮询
    def _schedule(self):
        if self.alive:
            self.top.after(self.POLL_MS, self._tick)

    def _tick(self):
        if not self.alive or self.busy or not self.key:
            return
        self.busy = True
        key = self.key

        def run():
            try:
                code, cookie = self.login.poll(key)
            except Exception:
                # 网络抖动不当成失败，下一轮继续问
                self.busy = False
                self._ui(lambda: self._set("网络不稳，正在重试…", "#b60"))
                self._schedule()
                return
            self.busy = False
            self._ui(lambda: self._handle(code, cookie))

        threading.Thread(target=run, daemon=True).start()

    def _handle(self, code, cookie):
        if not self.alive:
            return
        if code == 801:
            self._set("等待扫码…（二维码 5 分钟内有效）", "#333")
        elif code == 802:
            self._set("已扫码，请在手机上点「确认登录」", "#0a7")
        elif code == 803:
            if "MUSIC_U=" not in (cookie or ""):
                self._set("登录成功但没拿到 MUSIC_U，请改用粘贴方式", "#c33")
                return
            try:
                save_cookie(cookie)
            except OSError as e:
                self._set("保存失败：%s" % e, "#c33")
                return
            self._set("✅ 登录成功，正在确认账号…", "#0a7")
            nick = ""
            try:
                nick = self.login.nickname(cookie)
            except Exception:
                pass
            self._set("✅ 已登录%s，可以关闭本窗口了"
                      % (("：" + nick) if nick else ""), "#0a7")
            self.on_success(cookie, nick)
            return
        elif code == 800:
            self._set("二维码已过期，正在换一张…", "#b60")
            self.refresh()
            return
        else:
            self._set("未知状态（code=%s），换一张试试" % code, "#b60")
            return
        self._schedule()

    # ------------------------------------------------------------ 杂项
    def _set(self, text, color):
        if self.alive:
            self.status.config(text=text, fg=color)

    def _ui(self, fn):
        """把后台线程的结果送回 Tk 主线程（Tk 不是线程安全的）。"""
        if self.alive:
            try:
                self.top.after(0, fn)
            except Exception:
                pass

    def close(self):
        self.alive = False
        try:
            os.remove(QR_IMG)
        except OSError:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass


class Setup(object):
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("网易云登录 — RVC 点歌台")
        self.root.geometry("640x600")
        self.root.minsize(560, 520)
        self._build()
        saved = load_saved()
        if "MUSIC_U=" in saved:
            c, _ = extract(saved)
            self.txt.insert("1.0", c)
            self._status("检测到已保存的 Cookie，可点「检测登录态」看看还有没有效", "#8aa")

    def _build(self):
        pad = {"padx": 14, "pady": 4}
        tk.Label(self.root, text="网易云登录", font=("微软雅黑", 13, "bold")).pack(anchor="w", **pad)

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(2, 8))
        ttk.Button(top, text="📱 扫码登录（推荐）", command=self._qr).pack(side="left")

        t = tk.Text(self.root, height=8, wrap="word", relief="flat",
                    background="#f4f6fa", font=("微软雅黑", 9))
        t.insert("1.0", HELP_TEXT)
        t.config(state="disabled")
        t.pack(fill="x", padx=14, pady=(2, 8))

        tk.Label(self.root, text="或者把 Cookie 粘到这里：", font=("微软雅黑", 10)).pack(anchor="w", **pad)
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

    def _qr(self):
        QrWindow(self.root, NcmLogin(), self._qr_done)

    def _qr_done(self, cookie, nick):
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", cookie)
        self._status("✅ 扫码登录成功%s。网易云 API 会自动读取新 Cookie，不用重启"
                     % (("，账号：" + nick) if nick else ""), "#282")

    def _clear(self):
        if not messagebox.askyesno("确认", "删除已保存的 Cookie？\n（之后网易云 API 会退回匿名模式）"):
            return
        try:
            os.remove(COOKIE_FILE)
        except FileNotFoundError:
            pass
        self.txt.delete("1.0", "end")
        self._status("已清除。网易云 API 会退回匿名模式。", "#b60")

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
            save_cookie(c)
        except OSError as e:
            self._status("保存失败：%s" % e, "#c33")
            return
        self._status("已保存到 %s，正在检测登录态…" % os.path.basename(COOKIE_FILE), "#888")
        verify(c, self._after_verify)

    def _after_verify(self, s):
        ok = s.startswith("✅")
        if ok:
            s += "\n网易云 API 会自动读取新 Cookie，不用重启"
        self._status(s, "#282" if ok else "#c33")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Setup().run()
