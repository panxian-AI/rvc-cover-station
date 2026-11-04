# -*- coding: utf-8 -*-
"""公网访问控制：路径白名单、探测封禁、安全事件日志、安全视图。"""
import json
import os
import threading
import time

from .core import *          # noqa: F401,F403  （PORT / MSG_LOCAL_ONLY / SEC_LOG / probes）
from .core import SEC_LOG, _sec_last, _sec_lock
from . import core

# ---------------- 公网暴露面（fail-closed） ----------------
# 公网（隧道）能碰到的**只有**下面这些路径，其余一律当不存在处理并记一次探测。
# 为什么用白名单而不是黑名单：以后再加管理接口/调试接口时，忘了加保护也不会
# 自动暴露到隧道外面 —— 新路由默认是「公网不可见」。
PUBLIC_GET_EXACT = {"/", "/index.html", "/guest", "/console", "/favicon.ico",
                     "/admin", "/login", "/logout"}   # 后三个只做 302 回访客页
PUBLIC_GET_PREFIX = ("/api/guest/",)
PUBLIC_POST_EXACT = {"/api/guest/request", "/api/guest/wl_apply",
                     "/api/guest/unlock_request"}   # 解锁请求：限频 + 站长批准才生效

# 扫描/越权探测：同一 IP 10 分钟内碰 20 次非公开路径 → 封 30 分钟。
# 隧道地址一旦被扫到，全世界的小爬虫都会来试 /admin、/.env、/api/admin/...；
# 光回 403 太客气了（还会白烧 CPU），直接断一段时间。
PROBE_WINDOW = 600
PROBE_LIMIT = 20
PROBE_BAN = 1800
_probe = {}                       # ip -> [时间戳]
_ban = {}                         # ip -> 解封时间戳
_probe_lock = threading.Lock()
_probe_stat = {"total": 0}
_probe_recent = []                # [(时间戳, ip, 路径)]，最近 30 条，给控制台看

# 公网访问总开关：被刷爆 / 不想让人用了，控制台一键暂停，访客只看到维护页。
PUBLIC_STATE = (os.environ.get("RVC_PUBLIC_STATE")
                or os.path.join(HERE, "public_access.json"))
_pub_lock = threading.Lock()
_pub_cache = {"v": None}


def public_state():
    """公网访问开关的完整状态（给控制台看）。"""
    with _pub_lock:
        if _pub_cache["v"] is None:
            st = {"paused": False, "at": 0, "note": ""}
            try:
                with open(PUBLIC_STATE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    st = {"paused": bool(d.get("paused")), "at": float(d.get("at") or 0),
                          "note": str(d.get("note") or "")[:120]}
            except Exception:
                pass
            _pub_cache["v"] = st
        return dict(_pub_cache["v"])


def public_paused():
    """公网访客是否被暂停（本机控制台不受影响）。注意返回 bool。"""
    return bool(public_state().get("paused"))


def set_public_paused(flag, note=""):
    st = {"paused": bool(flag), "at": time.time(), "note": str(note or "")[:120]}
    with _pub_lock:
        try:
            tmp = PUBLIC_STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, PUBLIC_STATE)
        except OSError as e:
            return {"ok": False, "msg": "保存失败：%s" % e}
        _pub_cache["v"] = st
    sec_log("公网访问%s（站长操作）" % ("已暂停" if flag else "已恢复"))
    return {"ok": True, "paused": st["paused"],
            "msg": "已暂停公网访问：访客打开只会看到维护提示，本机控制台不受影响"
                   if flag else "已恢复公网访问"}


def _ban_left(ip):
    if not ip:
        return 0
    with _probe_lock:
        until = _ban.get(ip) or 0
        left = until - time.time()
        if left <= 0:
            _ban.pop(ip, None)
            return 0
        return int(left)


def _probe_hit(ip, path):
    """记一次「公网碰了不该碰的路径」。返回 (是否已封, 封禁剩余秒数)。"""
    if not ip:
        return False, 0
    now = time.time()
    with _probe_lock:
        _probe_stat["total"] += 1
        _probe_recent.append((now, ip, str(path or "")[:80]))
        del _probe_recent[:-30]
        hits = [t for t in _probe.get(ip, []) if now - t < PROBE_WINDOW]
        hits.append(now)
        _probe[ip] = hits
        if len(_probe) > 5000:
            for k in list(_probe)[:2500]:
                _probe.pop(k, None)
        if len(hits) >= PROBE_LIMIT:
            _ban[ip] = now + PROBE_BAN
            _probe[ip] = []
            banned = True
        else:
            banned = _ban.get(ip, 0) > now
        left = int(max(0, _ban.get(ip, 0) - now))
    if banned:
        sec_log("公网探测触发封禁 ip=%s path=%s 封 %d 分钟" % (ip, path, PROBE_BAN // 60), ip="")
    return banned, left


def security_view():
    """控制台「安全」卡片的数据。"""
    now = time.time()
    with _probe_lock:
        banned = [{"ip": k, "left": int(v - now)}
                  for k, v in _ban.items() if v > now]
        banned.sort(key=lambda x: -x["left"])
        recent = [{"t": time.strftime("%H:%M:%S", time.localtime(t)), "ip": i,
                   "path": p} for (t, i, p) in reversed(_probe_recent[-12:])]
        total = _probe_stat["total"]
        tracked = len(_probe)
    st = public_state()
    return {"ok": True, "paused": st["paused"], "paused_at": st["at"], "note": st["note"],
            "banned": banned[:20], "banned_count": len(banned),
            "probe_total": total, "tracked": tracked, "recent": recent,
            "public_paths": sorted(PUBLIC_GET_EXACT - {"/favicon.ico"}),
            "limits": {"probe_window": PROBE_WINDOW, "probe_limit": PROBE_LIMIT,
                       "ban_minutes": PROBE_BAN // 60}}



def sec_log(line, ip=""):
    """追加一行安全日志；文件超过 512KB 就只留后半段，避免被刷爆磁盘。

    同一来源 5 秒内只记一条：否则被人拿脚本狂打管理接口时，每一发请求都同步
    写一次盘，日志反倒成了新的放大器。
    """
    now = time.time()
    with _sec_lock:
        if ip and ip == _sec_last["ip"] and now - _sec_last["t"] < 5:
            return
        _sec_last["ip"], _sec_last["t"] = ip, now
    try:
        if os.path.isfile(SEC_LOG) and os.path.getsize(SEC_LOG) > 512 * 1024:
            with open(SEC_LOG, "r", encoding="utf-8", errors="replace") as f:
                f.seek(-256 * 1024, os.SEEK_END)
                keep = f.read()
            with open(SEC_LOG, "w", encoding="utf-8") as f:
                f.write(keep)
        with open(SEC_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + line + "\n")
    except OSError:
        pass
