# -*- coding: utf-8 -*-
"""概览页的「扫码登录网易云」：二维码直接画在网页里，不再弹独立窗口。

和 ncmapi/cookie_setup.py 共用 ncmapi/ncm_login.py 的网络逻辑，写的是同一个
ncm_cookie.txt；3002 代理按 mtime 热更新，所以扫完确认就生效，不用重启服务。

为什么把 ncmapi 当「文件」加载而不是 import 包：dashboard 与 ncmapi 是两个
平级目录，面板进程的 sys.path 里没有它。用 spec_from_file_location 显式加载，
既不污染 sys.path 的长期状态（只在加载时临时插一下），也不依赖当前工作目录。
"""
import importlib.util as _ilu
import os
import sys
import threading
import time

from . import core

_LOCK = threading.Lock()
_CACHE = {"path": "", "mod": None, "qr": None, "err": ""}
_STATUS = {"at": 0.0, "cookie": False, "nickname": "", "vip": False, "msg": ""}
STATUS_TTL = 60.0

# 二维码会话：key -> (NcmLogin, 创建时间)。一个 key 只轮询到成功/过期就丢。
_SESS = {}
SESS_TTL = 360.0
SESS_MAX = 40


def _load(path, name):
    spec = _ilu.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("加载失败：%s" % path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mods():
    """返回 (ncm_login, _qr)。目录缺失时抛 RuntimeError（页面显示「未安装」）。"""
    path = os.path.abspath(core.NCM_DIR or "")
    with _LOCK:
        if _CACHE["mod"] is not None and _CACHE["path"] == path:
            return _CACHE["mod"], _CACHE["qr"]
        lm = os.path.join(path, "ncm_login.py")
        qm = os.path.join(path, "_qr.py")
        if not (os.path.isfile(lm) and os.path.isfile(qm)):
            raise RuntimeError("网易云组件缺失（%s），重装可修复" % path)
        # _qr 是被 ncm_login 用 `import _qr` 找的，必须让它的目录可见
        added = path not in sys.path
        if added:
            sys.path.insert(0, path)
        try:
            qr = _load(qm, "rvc_ncm_qr")
            mod = _load(lm, "rvc_ncm_login")
        finally:
            if added:
                try:
                    sys.path.remove(path)
                except ValueError:
                    pass
        _CACHE.update({"path": path, "mod": mod, "qr": qr, "err": ""})
        return mod, qr


def installed():
    try:
        _mods()
        return True
    except Exception:
        return False


def status(verify=False):
    """概览页显示用：有没有 Cookie、是谁的号。

    verify=True 才会真的请求网易云确认登录态（~0.3s，网络不通时可能 10s），
    平时只看文件 + 上次结果缓存，别让概览页每次刷新都卡一下。
    """
    try:
        mod, _ = _mods()
    except Exception as e:
        return {"installed": False, "cookie": False, "nickname": "", "vip": False,
                "msg": str(e)}
    cookie = mod.has_cookie()
    now = time.time()
    if not cookie:
        _STATUS.update({"at": now, "cookie": False, "nickname": "", "vip": False,
                        "msg": "还没登录网易云"})
        return {"installed": True, "cookie": False, "nickname": "", "vip": False,
                "msg": _STATUS["msg"]}
    if verify or now - _STATUS["at"] > STATUS_TTL or not _STATUS["cookie"]:
        r = mod.account()
        _STATUS.update({"at": now, "cookie": bool(r.get("ok")),
                        "nickname": r.get("nickname") or "",
                        "vip": bool(r.get("vip")), "msg": r.get("msg") or ""})
        # Cookie 在文件里但网易云不认（过期）—— 页面要提示重新扫码
        return {"installed": True, "cookie": bool(r.get("ok")), "stale": not r.get("ok"),
                "nickname": _STATUS["nickname"], "vip": _STATUS["vip"],
                "msg": _STATUS["msg"]}
    return {"installed": True, "cookie": _STATUS["cookie"],
            "nickname": _STATUS["nickname"], "vip": _STATUS["vip"],
            "msg": _STATUS["msg"]}


def _trim():
    if len(_SESS) <= SESS_MAX:
        return
    now = time.time()
    for k, (_, ts) in list(_SESS.items()):
        if now - ts > SESS_TTL:
            _SESS.pop(k, None)
    while len(_SESS) > SESS_MAX:
        _SESS.pop(next(iter(_SESS)), None)


def new_qr():
    """申请一个 unikey 并画好二维码，返回 {ok, key, svg}。"""
    try:
        mod, qr = _mods()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    try:
        lg = mod.NcmLogin()
        key = lg.new_key()
    except Exception as e:
        return {"ok": False, "msg": "二维码生成失败：%s: %s（多半是被风控拦了，可以改用粘贴）"
                                    % (type(e).__name__, e)}
    try:
        mat, ver, _mask = qr.encode(mod.QR_TEXT_URL % key)
        svg = qr.to_svg(mat, border=3)
    except Exception as e:
        return {"ok": False, "msg": "二维码绘制失败：%s: %s" % (type(e).__name__, e)}
    with _LOCK:
        _SESS[key] = (lg, time.time())
        _trim()
    return {"ok": True, "key": key, "svg": svg, "version": ver, "size": len(mat),
            "ttl": int(SESS_TTL)}


def poll(key):
    """问一次状态。803 时立刻落盘并返回昵称。"""
    key = (key or "").strip()
    if not key or key not in _SESS:
        return {"ok": True, "code": 800, "msg": "二维码已失效，点「换一张」重来"}
    lg, _ts = _SESS[key]
    try:
        mod, _ = _mods()
        code, cookie = lg.poll(key)
    except Exception as e:
        # 网络抖动不算失败：前端继续轮询，别把二维码丢掉
        return {"ok": True, "code": 0, "retry": True,
                "msg": "网络不稳，正在重试…（%s）" % type(e).__name__}
    if code == 801:
        return {"ok": True, "code": 801, "msg": "等待扫码…（二维码 5 分钟内有效）"}
    if code == 802:
        return {"ok": True, "code": 802, "msg": "已扫码，请在手机上点「确认登录」"}
    if code == 800:
        _SESS.pop(key, None)
        return {"ok": True, "code": 800, "msg": "二维码过期了，点「换一张」"}
    if code == 803:
        _SESS.pop(key, None)
        if "MUSIC_U=" not in (cookie or ""):
            return {"ok": False, "code": 803,
                    "msg": "登录成功但没拿到 MUSIC_U，请改用粘贴方式"}
        try:
            mod.save_cookie(cookie)
        except (OSError, ValueError) as e:
            return {"ok": False, "code": 803, "msg": "保存失败：%s" % e}
        nick = ""
        try:
            nick = mod.NcmLogin().nickname(cookie)
        except Exception:
            pass
        with _LOCK:
            _STATUS.update({"at": time.time(), "cookie": True, "nickname": nick,
                            "vip": False, "msg": "已登录：%s" % (nick or "网易云账号")})
        return {"ok": True, "code": 803, "nickname": nick,
                "msg": "✅ 登录成功%s，已自动生效（不用重启服务）"
                       % (("：" + nick) if nick else "")}
    return {"ok": True, "code": code, "retry": True, "msg": "未知状态（code=%s）" % code}


def logout():
    """退出登录：删掉 Cookie 文件，代理下次请求就退回匿名模式。"""
    try:
        mod, _ = _mods()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    try:
        had = mod.clear_cookie()
    except OSError as e:
        return {"ok": False, "msg": "删除失败：%s" % e}
    with _LOCK:
        _SESS.clear()
        _STATUS.update({"at": time.time(), "cookie": False, "nickname": "",
                        "vip": False, "msg": "还没登录网易云"})
    return {"ok": True, "msg": "已退出网易云登录，搜索会退回匿名模式" if had
            else "本来就没有保存登录态"}


def save_raw(raw):
    """粘贴 Cookie 的备选入口：校验 → 落盘 → 查一次昵称。"""
    try:
        mod, _ = _mods()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    cookie, msg = mod.extract(raw)
    if not cookie:
        return {"ok": False, "msg": msg}
    r = mod.account(cookie)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "这个 Cookie 不能登录"}
    try:
        mod.save_cookie(cookie)
    except (OSError, ValueError) as e:
        return {"ok": False, "msg": "保存失败：%s" % e}
    with _LOCK:
        _STATUS.update({"at": time.time(), "cookie": True,
                        "nickname": r.get("nickname") or "",
                        "vip": bool(r.get("vip")), "msg": r.get("msg") or ""})
    return {"ok": True, "nickname": r.get("nickname") or "",
            "msg": "✅ 已保存并生效：%s" % (r.get("nickname") or "网易云账号")}
