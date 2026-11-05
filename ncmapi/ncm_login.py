# -*- coding: utf-8 -*-
"""网易云登录的「网络部分」：申请二维码、轮询状态、校验 Cookie、落盘。

这里刻意不 import tkinter —— 同一套逻辑有两个入口：

  1. dashboard/panelkit/ncmlogin.py   控制台概览页里内嵌的扫码卡片（主入口）
  2. ncmapi/cookie_setup.py          双击 bat 打开的独立小窗口（备选入口）

两条路写的是同一个 ncm_cookie.txt；3002 代理按文件 mtime 热更新，
所以不管从哪边登录，点完立刻生效，不用重启服务。
"""
import http.cookiejar
import json
import os
import re
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "ncm_cookie.txt")

BASE = "https://music.163.com"
VERIFY_URL = BASE + "/api/nuser/account/get"
QR_KEY_URL = BASE + "/api/login/qrcode/unikey"
QR_POLL_URL = BASE + "/api/login/qrcode/client/login"
QR_TEXT_URL = BASE + "/login?codekey=%s"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

# 本机直连网易云：走系统代理会把扫码轮询卡死（用户装过 Clash 之类时很常见）
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


def has_cookie():
    """只看「有没有 MUSIC_U」，不发网络请求（预检用）。"""
    return "MUSIC_U=" in load_saved()


def save_cookie(cookie):
    """原子写盘。注释行以 # 开头，ncm_proxy 读的时候会跳过。

    为什么不是直接 open(..., "w")：写到一半断电/被杀进程会留下半个 Cookie，
    代理读到的就是坏值，而且下次登录还会被当成「已登录」。先写 tmp 再 replace，
    最坏情况是旧文件原封不动。
    """
    cookie = " ".join((cookie or "").split())
    if "MUSIC_U=" not in cookie:
        raise ValueError("Cookie 里没有 MUSIC_U，不能当作登录态保存")
    text = ("# 由控制台扫码 / 粘贴保存，有 MUSIC_U 才算登录态\n" + cookie + "\n")
    tmp = COOKIE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, COOKIE_FILE)
    return COOKIE_FILE


def clear_cookie():
    try:
        os.remove(COOKIE_FILE)
        return True
    except FileNotFoundError:
        return False


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
        return account(cookie).get("nickname") or ""


def account(cookie=""):
    """查登录态：返回 {ok, nickname, vip, msg}。

    ok=False 既可能是「Cookie 失效」，也可能是「网络不通」—— 这两种对用户
    要做的事完全不同（重新扫码 / 检查网络），所以 msg 里分开说。
    """
    cookie = cookie if cookie is not None else load_saved()
    cookie = " ".join((cookie or "").split())
    if "MUSIC_U=" not in cookie:
        return {"ok": False, "nickname": "", "vip": False, "msg": "还没登录网易云"}
    try:
        req = urllib.request.Request(VERIFY_URL, headers={
            "Cookie": cookie, "User-Agent": UA, "Referer": BASE})
        with OPENER.open(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return {"ok": False, "nickname": "", "vip": False,
                "msg": "检测失败：%s: %s（网络不通也会这样）" % (type(e).__name__, e)}
    p = d.get("profile") or {}
    nick = p.get("nickname") or ""
    if nick:
        return {"ok": True, "nickname": nick, "vip": bool(p.get("vipType")),
                "msg": "已登录：%s" % nick}
    return {"ok": False, "nickname": "", "vip": False,
            "msg": "网易云收到了请求但没认出登录（Cookie 失效或不完整），重新扫码一次"}
