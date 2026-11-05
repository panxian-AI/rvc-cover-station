# -*- coding: utf-8 -*-
"""POST 路由：动作、审批、导入导出、更新。"""
import json
import os
import threading
import time
import urllib.parse

from .core import *          # noqa: F401,F403
from . import core, security, services, ncmlogin
from .security import (_ban, _ban_left, _probe, _probe_hit, _probe_lock,
                       public_paused, set_public_paused, sec_log,
                       PUBLIC_POST_EXACT)
from .services import start, start_all, stop, stop_all, start_tunnel, stop_tunnel


# ---------------- 数据备份的写操作 ----------------
def _data_action(path, body):
    """POST /api/data/* 的具体动作。

    导入是唯一会覆盖用户数据的操作，所以要求前端显式带 confirm=true ——
    面板上是「我已经确认要覆盖」那个勾选项，脚本直连也必须自己给出这个字段。
    """
    if not dataio_mod:
        return 500, {"ok": False, "msg": "数据备份模块未加载"}
    act = path[len("/api/data/"):].strip("/").lower()
    if act == "export":
        # 页面用它：先打包拿结果（能给出确定反馈），再单独下载那一份
        r = dataio_mod.export(with_audio=bool(body.get("audio")))
        return (200 if r.get("ok") else 400), r
    if act == "import_pick":
        return 200, dataio_mod.pick_zip()
    if act == "import_check":
        r = dataio_mod.check(body.get("path"))
        return (200 if r.get("ok") else 400), r
    if act == "import_apply":
        mode = str(body.get("mode") or "replace").lower()
        if mode not in ("replace", "merge"):
            return 400, {"ok": False, "msg": "未知的导入方式（只支持 replace / merge）"}
        if body.get("confirm") is not True:
            return 400, {"ok": False, "msg": "导入会改写本机数据，请先勾选确认再执行"}
        r = dataio_mod.apply(body.get("path"), token=body.get("token") or "", mode=mode)
        if r.get("ok"):
            sec_log("数据导入（%s）：%s" % (mode, r.get("msg")))
        return (200 if r.get("ok") else 400), r
    if act == "delete":
        r = dataio_mod.delete_backup(body.get("name"))
        return (200 if r.get("ok") else 400), r
    if act == "open":
        return 200, dataio_mod.open_dir()
    return 404, {"ok": False, "msg": "未知操作"}



def handle(h):
    parsed = urllib.parse.urlparse(h.path)
    path = parsed.path
    if not h._csrf_ok():
        h._send(403, json.dumps({"ok": False, "msg": "跨站请求已被拒绝"},
                                   ensure_ascii=False))
        return
    if path in ("/login", "/logout"):
        h._redirect("/")
        return
    ip, proxied = h._client()
    local = h._is_local()

    # ---- 公网第一道闸（fail-closed）：封禁 → 暂停 → 路径白名单 ----
    if not local:
        left = _ban_left(ip)
        if left:
            h._send(403, json.dumps({
                "ok": False, "msg": "这个网络访问过于频繁，已被临时限制（%d 分钟后自动解除）"
                                    % max(1, left // 60)}, ensure_ascii=False))
            return
        if public_paused():
            h._maintenance(as_json=True)
            return
        if path not in PUBLIC_POST_EXACT:
            _probe_hit(ip, path)
            h._local_only(as_json=True)
            return

    if guest_mod:
        gid, new_gid = guest_mod.gid_from(h.headers.get("Cookie"))
    else:
        gid, new_gid = "", False

    # 公开：访客提交翻唱申请 / 白名单申请 / 解锁请求（不需密码；解锁请求
    # 服务端限频且要站长批准才生效）
    if guest_mod and path in ("/api/guest/request", "/api/guest/wl_apply",
                              "/api/guest/unlock_request"):
        body = h._read_json()
        if h._too_big():
            return
        code, obj = guest_mod.handle_post(path, body, gid, ip,
                                          h.headers.get("CF-IPCountry"),
                                          h.headers.get("User-Agent"), False)
        hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
        h._send(code, json.dumps(obj, ensure_ascii=False), headers=hdr)
        return

    # 剩下的全是管理操作：只认本机直连
    if not h._local_only(as_json=True):
        return

    body = h._read_json()
    if h._too_big():
        return
    # 数据备份：选包 / 预检 / 导入 / 删备份 / 打开目录
    # 前缀 /api/data/ 与 guest 的 /api/admin/ 不重叠，但仍要排在那条通配之前
    if dataio_mod and path.startswith("/api/data/"):
        try:
            code, obj = _data_action(path, body)
        except Exception as e:
            code, obj = 500, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(code, json.dumps(obj, ensure_ascii=False))
        return
    # 模型市场：下载并安装（必须排在 guest 的 /api/admin/ 通配之前，也不与它同前缀）
    if market and path.startswith("/api/market/"):
        code, obj = market.handle_post(path, body)
        h._send(code, json.dumps(obj, ensure_ascii=False))
        return
    # 模型导入：选文件 / 识别 / 安装（必须排在 guest 的 /api/admin/ 通配之前）
    if model_import and path.startswith("/api/admin/import_"):
        try:
            code, obj = model_import.handle_post(path, body)
        except Exception as e:
            code, obj = 500, {"ok": False, "msg": str(e)}
        h._send(code, json.dumps(obj, ensure_ascii=False))
        return
    # 模型管理：删除声线/索引（两段式，先预览再 confirm）
    if models_mgr and path.startswith("/api/models/"):
        try:
            code, obj = models_mgr.handle_post(path, body)
        except Exception as e:
            code, obj = 500, {"ok": False, "msg": str(e)}
        h._send(code, json.dumps(obj, ensure_ascii=False))
        return
    # 高级设置：读 / 存 / 恢复默认（必须排在 guest 的 /api/admin/ 通配之前）
    if settings_mod and path == "/api/admin/settings":
        act = str(body.get("act") or "save").lower()
        try:
            if act == "reset":
                obj = settings_mod.reset()
            else:
                vals = body.get("values")
                if not isinstance(vals, dict):
                    vals = {k: v for k, v in body.items() if k in settings_mod._BY_KEY}
                obj = settings_mod.save(vals)
        except Exception as e:
            obj = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(200 if obj.get("ok") else 400,
                   json.dumps(obj, ensure_ascii=False))
        return
    # 网易云登录（概览页内嵌扫码）：申请二维码 / 粘贴 Cookie / 退出登录
    if path.startswith("/api/ncm/login/"):
        act = path[len("/api/ncm/login/"):].strip("/").lower()
        try:
            if act == "qr":
                obj = ncmlogin.new_qr()
            elif act == "cookie":
                obj = ncmlogin.save_raw(body.get("raw") or "")
            elif act == "logout":
                obj = ncmlogin.logout()
            else:
                obj = {"ok": False, "msg": "未知操作"}
        except Exception as e:
            obj = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(200 if obj.get("ok") else 400, json.dumps(obj, ensure_ascii=False))
        return
    # 版本更新：下载最新 tag 的源码覆盖代码目录（不碰数据与模型）
    if path == "/api/update/apply":
        try:
            obj = updater.apply() if updater else {"ok": False, "msg": "版本检测模块未加载"}
        except Exception as e:
            obj = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(200 if obj.get("ok") else 400, json.dumps(obj, ensure_ascii=False))
        return
    # 安全操作：暂停/恢复公网访问、解除误封（必须排在 guest 的 /api/admin/ 通配之前）
    if path == "/api/admin/security":
        act = str(body.get("act") or "").lower()
        if act in ("pause", "resume"):
            obj = set_public_paused(act == "pause", body.get("note") or "")
        elif act == "unban":
            ipx = str(body.get("ip") or "").strip()
            with _probe_lock:
                n = 1 if _ban.pop(ipx, None) else 0
                if ipx:
                    _probe.pop(ipx, None)
            obj = {"ok": bool(n), "msg": "已解除 %s 的限制" % ipx if n else "这个 IP 不在限制列表里"}
        else:
            obj = {"ok": False, "msg": "未知操作"}
        h._send(200 if obj.get("ok") else 400,
                   json.dumps(obj, ensure_ascii=False))
        return
    # 管理员：审批
    if guest_mod and path.startswith("/api/admin/"):
        code, obj = guest_mod.handle_post(path, body, gid, ip,
                                          h.headers.get("CF-IPCountry"),
                                          h.headers.get("User-Agent"), True)
        h._send(code, json.dumps(obj, ensure_ascii=False))
        return

    if not (path == "/api/action" or (cover_mod and path.startswith("/api/cover/"))):
        h._send(404, json.dumps({"error": "not found"}))
        return

    if path != "/api/action":
        try:
            code, obj = cover_mod.handle_post(path, body)
        except Exception as e:
            code, obj = 500, {"ok": False, "msg": str(e)}
        h._send(code, json.dumps(obj, ensure_ascii=False))
        return

    act, target = body.get("do") or body.get("action"), body.get("target")
    force = bool(body.get("force"))
    if act == "start":
        # v1.2.1 起 3333 不再依赖 WebUI 先就绪（打开页面会自动拉模型列表），
        # 单独「启动后端」不再拦截；WebUI 没起时换声任务会报可读的错误。
        ok, msg = services.start(target)
    elif act == "start_all":
        ok, msg = services.start_all()
    elif act == "stop_all":
        # 只停服务，不关公网隧道：控制台这个按钮的语义是「面板留着」，
        # 隧道是通向面板的，一起关掉公网就再也打不开了。
        ok, msg = services.stop_all(include_tunnel=False)
    elif act == "shutdown":
        ok, msg = services.stop_all()
        if not ok:
            # 关键：没停干净就不要退出面板。以前这里不管成败都 os._exit，
            # 用户就再也点不到停止，只能去任务管理器——「没法完全退出」。
            h._send(200, json.dumps({
                "ok": False,
                "msg": msg + "。还有进程没停掉，面板先不退出，可以再点一次；"
                             "仍不行就双击「停止RVC.bat」强制结束。",
                "shutdown": False}, ensure_ascii=False))
            return
        msg = (msg + "；面板即将关闭，下次用请双击「启动RVC.bat」") if ok else msg
        h._send(200, json.dumps({"ok": ok, "msg": msg, "shutdown": True},
                                   ensure_ascii=False))
        # 先把响应发完再退，否则浏览器拿不到消息
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return
    elif act == "stop":
        ok, msg = services.stop(target)
    elif act == "tunnel_start":
        ok, msg = services.start_tunnel()
    elif act == "tunnel_stop":
        ok, msg = services.stop_tunnel()
    elif act == "restart":
        ok1, m1 = services.stop(target)
        time.sleep(3)
        ok2, m2 = services.start(target)
        ok, msg = ok2, "%s / %s" % (m1, m2)
    else:
        ok, msg = False, "未知操作: %s" % act
    h._send(200, json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))

