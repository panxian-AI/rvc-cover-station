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


def _status_obj():
    mod = _sources()
    items = []
    for m in mod.meta():
        st = mod.login_status(m["key"])
        store = mod.store(m["key"])
        items.append({
            "key": m["key"], "label": m["label"], "hint": m["hint"],
            "cookie_file": m["cookie_file"],
            "has_cookie": bool(store.logged_in()),
            "login": bool(st.get("login")),
            "nickname": st.get("nickname") or "",
            "msg": st.get("msg") or "",
            "is_default": m["key"] == default(),
        })
    return {"ok": True, "default": default(), "platforms": items}


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
    """保存某个平台的 Cookie，顺手做一次真实登录态检查。"""
    try:
        mod = _sources()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    key = mod.normalize(platform)
    try:
        mod.store(key).save(raw)
    except (ValueError, OSError) as e:
        return {"ok": False, "msg": str(e)}
    _invalidate()
    st = mod.login_status(key, force=True)
    return {"ok": True, "platform": key, "login": bool(st.get("login")),
            "nickname": st.get("nickname") or "",
            "msg": ("已保存；" + (st.get("msg") or "")) if st.get("msg")
                   else "已保存并生效"}


def clear_cookie(platform):
    try:
        mod = _sources()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    key = mod.normalize(platform)
    try:
        had = mod.store(key).clear()
    except OSError as e:
        return {"ok": False, "msg": "删除失败：%s" % e}
    _invalidate()
    return {"ok": True, "platform": key,
            "msg": "已清掉 %s 的 Cookie，回到匿名模式" % mod.get(key).LABEL if had
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
