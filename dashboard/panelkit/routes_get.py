# -*- coding: utf-8 -*-
"""GET 路由：页面 + 查询接口。

每个分支都直接由 handler 发响应（h._send 等），所以这里是「末端的 if 链」，
不存在返回值 —— 跟老 panel.py 里的写法一一对应，改起来不容易漏。
"""
import json
import urllib.parse

from .core import *          # noqa: F401,F403
from . import core, pages, security, sysinfo, services, ncmlogin, music
from .security import (_ban_left, _probe_hit, public_paused, security_view,
                       PUBLIC_GET_EXACT, PUBLIC_GET_PREFIX)
from .sysinfo import collect, panel_log_tail
from .services import public_info


def handle(h):
    parsed = urllib.parse.urlparse(h.path)
    path = parsed.path
    q = urllib.parse.parse_qs(parsed.query)
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
            h._maintenance(as_json=path.startswith("/api/"))
            return
        if not (path in PUBLIC_GET_EXACT or path.startswith(PUBLIC_GET_PREFIX)):
            # 既不是公开路径 → 记一次探测，再按「管理功能只在本机」拒掉
            _probe_hit(ip, path)
            h._local_only(as_json=path.startswith("/api/"))
            return
        # 明确拒绝路径穿越/危险字符（正常访客不会这么请求）
        if ".." in path or "%2e" in path.lower() or "\x00" in path:
            _probe_hit(ip, path)
            h._send(400, json.dumps({"error": "bad request"}))
            return

    if guest_mod:
        gid, new_gid = guest_mod.gid_from(h.headers.get("Cookie"))
    else:
        gid, new_gid = "", False

    # ---- 首页：本机是控制台，公网/局域网来客只能看访客页 ----
    if path in ("/", "/index.html", "/console"):
        if local:
            h._send(200, pages.page("console"), "text/html; charset=utf-8")
            return
        if path == "/console":
            h._redirect("/")           # 外面打开 /console 就回访客页
            return
        # 公网来客：只能看到访客点歌页
        if guest_mod:
            guest_mod.touch(gid, ip, h.headers.get("CF-IPCountry"),
                            h.headers.get("User-Agent"))
            hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
            h._send(200, guest_mod.page(), "text/html; charset=utf-8", hdr)
        else:
            h._send(500, "访客页未加载：guest.py 导入失败", "text/plain; charset=utf-8")
        return
    # 访客页固定入口（本机也能预览；公网也是这个页面）
    if guest_mod and path == "/guest":
        if not local:
            guest_mod.touch(gid, ip, h.headers.get("CF-IPCountry"),
                            h.headers.get("User-Agent"))
        hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
        h._send(200, guest_mod.page(), "text/html; charset=utf-8", hdr)
        return
    if guest_mod and path == "/api/guest/file":
        rid = (q.get("rid") or [""])[0]
        alt = ((q.get("gid") or [""])[0] if isinstance(q.get("gid"), list)
               else (q.get("gid") or ""))
        p = guest_mod.file_for(gid, rid, alt_gid=alt)
        if p:
            h._send_file(p)
        else:
            h._send(404, json.dumps({"ok": False, "msg": "还没有可下载的成品"},
                                       ensure_ascii=False))
        return
    if guest_mod and path.startswith("/api/guest/"):
        code, obj = guest_mod.handle_get(path, q, gid, False, ip)
        # 新访客直接打接口（或 Cookie 失效）也要把访客 id 种下去，
        # 否则验证码绑定的 gid 每次都变，永远过不了
        hdr = [("Set-Cookie", guest_mod.gid_cookie(gid, proxied))] if new_gid else []
        h._send(code, json.dumps(obj, ensure_ascii=False), headers=hdr)
        return

    # 旧的密码入口已经取消，书签跳回来即可
    if path in ("/login", "/logout", "/admin"):
        h._redirect("/")
        return

    # 浏览器每个页面都会自动请求 favicon：必须在「只认本机」之前处理，
    # 否则每个公网访客都吃一次 403 并往 security.log 写一行「拒绝远程管理」，
    # 真正的扫描事件反而被噪声淹没。
    if path == "/favicon.ico":
        h._send(204, b"")
        return
    # 就绪探针：只回一个标识，不暴露任何本机信息。
    # 启动器用它区分「面板起来了」和「端口被别的程序占了」——
    # 以前只测 TCP 能不能连，别的程序占着 8899 也会被当成面板已就绪。
    if path == "/api/ping":
        h._send(200, json.dumps({"ok": True, "app": "rvc-panel"}, ensure_ascii=False))
        return

    # ---- 以下全是管理功能：只认本机直连 ----
    if not h._local_only(as_json=path.startswith("/api/")):
        return

    # 有写盘副作用的 GET（导出会真的打包 zip）：跨站页面不能借本机身份触发。
    if path == "/api/data/export" and not h._same_origin():
        h._send(403, json.dumps({"ok": False, "msg": "跨站请求已被拒绝"},
                                   ensure_ascii=False))
        return

    # 多页面共用的样式/脚本（只有本机能看，公网访客页是自包含的）
    if path.startswith("/assets/"):
        h._send_asset(path[len("/assets/"):])
        return

    # 控制台的各个分页：/console/tasks、/console/guest、/console/library、/console/settings
    _pk = pages.key_for(path)
    if _pk and _pk != "console":
        h._send(200, pages.page(_pk), "text/html; charset=utf-8")
        return

    if path in ("/panel",):
        h._send(200, pages.page("legacy"), "text/html; charset=utf-8")
    elif path == "/cover":
        if not cover_mod:
            h._send(500, "翻唱页未加载：cover.py 导入失败，看面板控制台输出",
                       "text/plain; charset=utf-8")
        else:
            try:
                h._send(200, cover_mod.page(), "text/html; charset=utf-8")
            except Exception as e:
                h._send(500, "翻唱页读取失败: %s" % e, "text/plain; charset=utf-8")
    elif path == "/api/cover/file":
        name = (q.get("name") or [""])[0]
        p = cover_mod.file_abs(name) if cover_mod else None
        if p:
            h._send_file(p)
        else:
            h._send(404, json.dumps({"ok": False, "msg": "文件不存在"}, ensure_ascii=False))
    elif cover_mod and path.startswith("/api/cover/"):
        try:
            code, obj = cover_mod.handle_get(path, q)
        except Exception as e:
            code, obj = 500, {"ok": False, "msg": str(e)}
        h._send(code, json.dumps(obj, ensure_ascii=False))
    elif path == "/market":
        h._send(200, pages.page("market"), "text/html; charset=utf-8")
    elif path == "/models":
        if models_mgr:
            h._send(200, models_mgr.page(), "text/html; charset=utf-8")
        else:
            h._send(500, "模型管理未加载：models_mgr.py 导入失败", "text/plain; charset=utf-8")
    elif models_mgr and path == "/api/models/list":
        try:
            h._send(200, json.dumps(models_mgr.list_models(), ensure_ascii=False))
        except Exception as e:
            h._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
    elif market and path.startswith("/api/market/"):
        code, obj = market.handle_get(path, q)
        h._send(code, json.dumps(obj, ensure_ascii=False))
    elif settings_mod and path == "/api/admin/settings":
        # 必须排在 guest 的 /api/admin/ 通配之前，否则会被它截胡成 404
        try:
            h._send(200, json.dumps(settings_mod.ui(), ensure_ascii=False))
        except Exception as e:
            h._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
    elif path == "/api/admin/security":
        # 同样必须排在 guest 的 /api/admin/ 通配之前
        h._send(200, json.dumps(security_view(), ensure_ascii=False))
    elif guest_mod and path.startswith("/api/admin/"):
        code, obj = guest_mod.handle_get(path, q, gid, True, ip)
        h._send(code, json.dumps(obj, ensure_ascii=False))
    elif path == "/api/data/summary":
        if not dataio_mod:
            h._send(500, json.dumps({"ok": False, "msg": "数据备份模块未加载"},
                                       ensure_ascii=False))
        else:
            try:
                h._send(200, json.dumps(dataio_mod.summary(), ensure_ascii=False))
            except Exception as e:
                h._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
    elif path == "/api/data/export":
        # 点一下就把备份打包好并直接下载（浏览器存到「下载」文件夹）
        if not dataio_mod:
            h._send(500, json.dumps({"ok": False, "msg": "数据备份模块未加载"},
                                       ensure_ascii=False))
        else:
            audio = str((q.get("audio") or ["0"])[0]).lower() in ("1", "true", "yes")
            try:
                r = dataio_mod.export(with_audio=audio)
            except Exception as e:
                r = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
            if r.get("ok"):
                h._send_download(r["path"], r["name"])
            else:
                h._send(400, json.dumps(r, ensure_ascii=False))
    elif path == "/api/data/file":
        # 备份列表里点「下载」拿某一份历史备份
        p = dataio_mod.backup_abs((q.get("name") or [""])[0]) if dataio_mod else None
        if p:
            h._send_download(p)
        else:
            h._send(404, json.dumps({"ok": False, "msg": "这份备份不在了"},
                                       ensure_ascii=False))
    elif path == "/api/update/check":
        force = str((q.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
        try:
            obj = updater.check(force=force) if updater else {
                "ok": False, "current": "?", "has_update": False,
                "error": "版本检测模块未加载"}
        except Exception as e:
            obj = {"ok": False, "current": "?", "has_update": False,
                   "error": "%s: %s" % (type(e).__name__, e)}
        h._send(200, json.dumps(obj, ensure_ascii=False))
    # 多平台音源：各平台 Cookie 状态 + 默认平台（不发网络请求的那部分在 music.py 里缓存）
    elif path == "/api/music/platforms":
        force = str((q.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
        try:
            obj = music.status(force=force)
        except Exception as e:
            obj = {"ok": False, "platforms": [], "default": "netease",
                   "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(200, json.dumps(obj, ensure_ascii=False))
    # 网易云登录（概览页内嵌扫码）：状态 / 轮询。
    # poll 在 803 时会写 ncm_cookie.txt —— 有写盘副作用的 GET，同样不许跨站触发。
    elif path == "/api/ncm/login/status":
        verify = str((q.get("verify") or ["0"])[0]).lower() in ("1", "true", "yes")
        try:
            obj = ncmlogin.status(verify=verify)
        except Exception as e:
            obj = {"installed": False, "cookie": False, "nickname": "",
                   "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(200, json.dumps(obj, ensure_ascii=False))
    elif path == "/api/ncm/login/poll":
        if not h._same_origin():
            h._send(403, json.dumps({"ok": False, "msg": "跨站请求已被拒绝"},
                                       ensure_ascii=False))
            return
        try:
            obj = ncmlogin.poll((q.get("key") or [""])[0])
        except Exception as e:
            obj = {"ok": False, "code": 0, "msg": "%s: %s" % (type(e).__name__, e)}
        h._send(200, json.dumps(obj, ensure_ascii=False))
    elif path == "/api/status":
        try:
            st = collect()
            # 访客库写盘失败（磁盘满 / 文件被占用）不能静默：概览页要看得见
            try:
                if guest_mod and hasattr(guest_mod, "save_error"):
                    err = guest_mod.save_error()
                    if err:
                        st["save_error"] = err
            except Exception:
                pass
            h._send(200, json.dumps(st, ensure_ascii=False))
        except Exception as e:
            h._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
    elif path == "/api/deps":
        try:
            d = deps_mod.check() if deps_mod else {
                "ok": False, "items": [], "drift": [], "missing_file": True,
                "msg": "依赖核对模块未加载（deps.py 缺失）"}
            # 只有「装了但版本不对」才给修复命令；清单本身缺失时给命令没用
            if deps_mod and not d.get("ok") and not d.get("missing_file"):
                d["repair"] = deps_mod.repair_cmd()
            h._send(200, json.dumps(d, ensure_ascii=False))
        except Exception as e:
            h._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
    elif path == "/api/public":
        try:
            h._send(200, json.dumps(public_info(), ensure_ascii=False))
        except Exception as e:
            h._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
    elif path == "/api/panel_log":
        try:
            h._send(200, json.dumps(panel_log_tail(), ensure_ascii=False))
        except Exception as e:
            h._send(500, json.dumps({"file": None, "lines": [str(e)], "size": 0},
                                       ensure_ascii=False))
    elif path == "/favicon.ico":
        h._send(204, b"")          # 兜底（正常在上面已处理，本机不该走到这）
    else:
        h._send(404, json.dumps({"error": "not found"}))
