# -*- coding: utf-8 -*-
"""面板侧的多平台音源：Cookie 存取、状态查询、默认平台。

为什么不在面板进程里 `import sources`：dashboard 和 ncmapi 是两个平级目录，
面板的 sys.path 里没有后者；直接插路径再 import 会留下一个全局 `sources`
（测试里换目录就串了）。这里按文件位置加载成独立命名空间，换目录能重载。
"""
import importlib.util as _ilu
import os
import sys
import threading
import time

from . import core

_LOCK = threading.Lock()
_CACHE = {"dir": "", "mod": None}
_STATUS = {"at": 0.0, "obj": None}
STATUS_TTL = 60.0

META_FALLBACK = [
    {"key": "netease", "label": "网易云音乐"},
    {"key": "qq", "label": "QQ 音乐"},
    {"key": "kugou", "label": "酷狗音乐"},
]


def _sources():
    """加载 ncmapi/sources 包；目录缺失抛 RuntimeError。"""
    root = os.path.abspath(core.NCM_DIR or "")
    pkg = os.path.join(root, "sources", "__init__.py")
    with _LOCK:
        if _CACHE["mod"] is not None and _CACHE["dir"] == root:
            return _CACHE["mod"]
        if not os.path.isfile(pkg):
            raise RuntimeError("音源组件缺失（%s），重装可修复" % os.path.join(root, "sources"))
        spec = _ilu.spec_from_file_location(
            "rvc_music_sources", pkg, submodule_search_locations=[os.path.dirname(pkg)])
        mod = _ilu.module_from_spec(spec)
        # 包内的 `from . import kugou` 要靠 sys.modules 找到自己；
        # 不注册就会 ModuleNotFoundError: No module named 'rvc_music_sources'
        sys.modules["rvc_music_sources"] = mod
        spec.loader.exec_module(mod)
        _CACHE.update(dir=root, mod=mod)
        _STATUS.update(at=0.0, obj=None)
        return mod


def installed():
    try:
        _sources()
        return True
    except Exception:
        return False


def default():
    try:
        return core.settings_mod.get().get("music_platform") or "netease"
    except Exception:
        return "netease"


def aggregate():
    """聚合搜索开关（默认开）：一次打三家、结果交错。"""
    try:
        v = core.settings_mod.get().get("music_aggregate")
    except Exception:
        return True
    return True if v is None else bool(v)


def _status_obj():
    mod = _sources()
    items = []
    for m in mod.meta():
        key = m["key"]
        st = mod.login_status(key)          # 汇总（内部已按账号探测 + 60s 缓存）
        accs = mod.accounts(key)            # 逐账号明细（同一份缓存）
        items.append({
            "key": key, "label": m["label"], "hint": m["hint"],
            "cookie_dir": m.get("cookie_dir") or "",
            "cookie_file": m.get("cookie_file") or "",
            "has_cookie": bool(accs),
            "login": bool(st.get("login")),
            "nickname": st.get("nickname") or "",
            "msg": st.get("msg") or "",
            "count": len(accs),
            "ready": int(st.get("ready") or 0),
            "accounts": [{"id": a["id"], "legacy": bool(a.get("legacy")),
                          "valid": bool(a.get("valid")),
                          "login": bool(a.get("login")),
                          "nickname": a.get("nickname") or "",
                          "vip": a.get("vip"), "msg": a.get("msg") or ""}
                         for a in accs],
            "is_default": key == default(),
        })
    return {"ok": True, "default": default(), "platforms": items,
            "aggregate": aggregate()}


def status(force=False):
    """给概览页：每个平台「配没配 Cookie、能不能取流」。缓存 60 秒。"""
    now = time.time()
    if not force and _STATUS["obj"] and now - _STATUS["at"] < STATUS_TTL:
        return _STATUS["obj"]
    try:
        obj = _status_obj()
    except Exception as e:
        return {"ok": False, "default": "netease", "platforms": [],
                "msg": "%s: %s" % (type(e).__name__, e)}
    _STATUS.update(at=now, obj=obj)
    return obj


def _invalidate():
    _STATUS.update(at=0.0, obj=None)


def save_cookie(platform, raw):
    """存一个账号（同一账号会覆盖），顺手做一次真实登录态检查。"""
    try:
        mod = _sources()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    key = mod.normalize(platform)
    book = mod.store(key)
    existed = set(x["id"] for x in book.accounts())
    try:
        got = book.save(raw)
    except (ValueError, OSError) as e:
        return {"ok": False, "msg": str(e)}
    _invalidate()
    st = mod.login_status(key, force=True)
    accs = mod.accounts(key, force=True)
    acc = next((a for a in accs if a["id"] == got["id"]), None)
    label = mod.get(key).LABEL
    head = "已更新账号" if got["id"] in existed else "已添加账号"
    tail = ""
    if acc and acc.get("msg"):
        tail = "；" + acc["msg"]
    elif acc and acc.get("login"):
        tail = "；✅ 可用"
    return {"ok": True, "platform": key, "account": got["id"],
            "count": len(accs), "login": bool(acc and acc.get("login")),
            "nickname": (acc or {}).get("nickname") or "",
            "msg": "%s %s（%s 现在共 %d 个）%s" % (head, got["id"], label, len(accs), tail)}


def logout(platform, account):
    """退出一个账号（删掉它的 Cookie 文件）。"""
    try:
        mod = _sources()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    key = mod.normalize(platform)
    acc_id = str(account or "").strip()
    if not acc_id:
        return {"ok": False, "msg": "没说退出哪个账号"}
    try:
        had = mod.logout(key, acc_id)
    except OSError as e:
        return {"ok": False, "msg": "删除失败：%s" % e}
    _invalidate()
    left = len(mod.store(key).entries())
    if left:
        mod.login_status(key, force=True)      # 让剩下的账号重新探活
    label = mod.get(key).LABEL
    if not had:
        msg = "这个账号本来就不在了（%s 还有 %d 个）" % (label, left)
    elif left:
        msg = "已退出该账号，%s 还剩 %d 个（会自动换下一个）" % (label, left)
    else:
        msg = "已退出 %s 最后一个账号，回到匿名模式" % label
    return {"ok": True, "platform": key, "removed": bool(had), "left": left,
            "msg": msg}


def clear_cookie(platform):
    """清掉这个平台的所有账号。"""
    try:
        mod = _sources()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    key = mod.normalize(platform)
    try:
        n = mod.logout_all(key)
    except OSError as e:
        return {"ok": False, "msg": "删除失败：%s" % e}
    _invalidate()
    return {"ok": True, "platform": key, "removed": int(n or 0),
            "msg": ("已清掉 %s 的 %d 个账号，回到匿名模式" % (mod.get(key).LABEL, n)) if n
                   else "本来就没有保存 Cookie"}


def set_default(platform):
    try:
        mod = _sources()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    key = mod.normalize(platform)
    r = core.settings_mod.save({"music_platform": key})
    _invalidate()
    if r.get("ok"):
        r["msg"] = "默认音源已改为 %s" % mod.get(key).LABEL
    return r


def platforms_meta():
    """给搜索下拉框：不带 Cookie 状态，不发网络请求。"""
    try:
        return _sources().meta()
    except Exception:
        return list(META_FALLBACK)
