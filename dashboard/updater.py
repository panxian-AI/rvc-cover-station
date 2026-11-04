# -*- coding: utf-8 -*-
"""版本检测与在线更新。

为什么单独一个模块：
    控制台每次打开要能知道「是不是最新版」，有新版本还要能一键更新。
    这两件事都要联网、都要碰磁盘，放在 panel.py 里会把路由代码淹掉。

设计要点：
    · 本地版本读仓库根的 VERSION 文件（缺失时回落到本文件的 FALLBACK，老安装也能显示）；
    · 最新版本查 GitHub Releases（/releases/latest），结果缓存 10 分钟并落盘，
      免得每次刷新页面都打一次 API（匿名额度只有 60 次/小时）；
    · 更新 = 下载该 tag 的源码 zip → 按目录映射覆盖代码（dashboard / RVCSVC-API /
      ncmapi / setup / RVC-WebUI），**用户数据与模型一律不碰**（它们在 .gitignore 里，
      zip 里根本没有；另外还有一层白名单外的跳过规则兜底）；
    · 覆盖前把要改的文件备份到 _update_backup/<时间戳>/，出问题能手工找回；
    · 失败一律返回 {ok: False, msg: 中文原因}，绝不抛异常到路由层。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from paths import DATA, DASH, PROJ, RVC_ROOT

import speed

REPO = "panxian-AI/rvc-cover-station"
FALLBACK_VERSION = "1.4.0"          # 没有 VERSION 文件时（老安装）显示这个
VERSION_FILE = os.path.join(PROJ, "VERSION")
CACHE_FILE = os.path.join(DASH, "update_cache.json")
BACKUP_DIR = os.path.join(PROJ, "_update_backup")

CACHE_TTL = 600                      # 秒：同一个结果 10 分钟内不重复查
HTTP_TIMEOUT = 12
MAX_ZIP = 220 * 1024 * 1024          # 更新包大小上限，防被塞大文件

_UA = {"User-Agent": "rvc-cover-station-updater"}

# 代码目录 → 安装布局里的位置（和 一键安装.bat 的布局一致）
# 只覆盖这几处：entry bat / 个人数据 / 模型 / venv 全都不动。
DIR_MAP = [
    ("dashboard", DASH),
    ("RVCSVC-API", os.path.join(PROJ, "RVCSVC-API")),
    ("ncmapi", os.path.join(PROJ, "ncmapi")),
    ("setup", os.path.join(PROJ, "setup")),
    ("RVC-WebUI", RVC_ROOT),
]

# 这些路径绝不能被更新覆盖（个人数据 / 模型 / 虚拟环境 / 缓存）
SKIP_PARTS = {
    "__pycache__", ".venv", "venv", "apienv", "node_modules",
    "logs", "log", "output", "temp", "tmp", "gradio_tmp", "TEMP",
    "dataset", "weights", "indices", "rmvpe", "pymss_weights",
    "covers", "_update_backup", "_tts_work",
}
SKIP_NAMES = {
    "guest_db.json", "advanced_settings.json", "ncm_cookie.txt", "security.log",
    "tunnel.log", "update_cache.json", "auth.json",
}
SKIP_EXT = (".pyc", ".pth", ".index", ".pt", ".log", ".wav", ".mp3", ".exe",
            ".ckpt", ".bin", ".safetensors")

_cache = {"at": 0.0, "data": None}


# ---------------- 版本号 ----------------
def local_version() -> str:
    """当前版本（VERSION 文件 → 代码里的 FALLBACK）。"""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
        if re.fullmatch(r"\d+(\.\d+){0,3}", v):
            return v
    except OSError:
        pass
    return FALLBACK_VERSION


def _tuple(v) -> tuple:
    parts = []
    for p in str(v or "").strip().lstrip("vV").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest: str, current: str) -> bool:
    return _tuple(latest) > _tuple(current)


# ---------------- 网络 ----------------
def _get(url: str, timeout=HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_release() -> dict:
    raw = _get("https://api.github.com/repos/%s/releases/latest" % REPO)
    d = json.loads(raw.decode("utf-8", "ignore"))
    tag = str(d.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("GitHub 没有返回版本号")
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "name": d.get("name") or tag,
        "notes": d.get("body") or "",
        "url": d.get("html_url") or ("https://github.com/%s/releases/tag/%s" % (REPO, tag)),
        "published_at": d.get("published_at") or "",
        "asset": (d.get("assets") or [{}])[0].get("browser_download_url") or "",
        "asset_name": (d.get("assets") or [{}])[0].get("name") or "",
    }


def _load_disk_cache() -> dict | None:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("tag") and time.time() - float(d.get("_at") or 0) < CACHE_TTL:
            return d
    except Exception:
        pass
    return None


def _save_disk_cache(d: dict) -> None:
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(d, _at=time.time()), f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


# ---------------- 检测 ----------------
def check(force: bool = False) -> dict:
    """查最新版本。force=True 忽略内存缓存（磁盘缓存仍用，避免刷爆额度）。"""
    current = local_version()
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < CACHE_TTL:
        return _wrap(current, _cache["data"], cached=True)

    rel = None
    if not force:
        disk = _load_disk_cache()
        if disk:
            rel = {k: disk.get(k) for k in ("tag", "version", "name", "notes", "url",
                                            "published_at", "asset", "asset_name")}
    if rel is None:
        try:
            rel = _fetch_release()
        except Exception as e:
            # 查不到就老实说查不到：不影响控制台其它功能
            err = _explain(e)
            if _cache["data"]:      # 有旧结果就先拿旧的顶上，标注已过期
                out = _wrap(current, _cache["data"], cached=True)
                out["error"] = err
                return out
            return {"ok": False, "current": current, "has_update": False,
                    "error": err, "msg": "版本检测失败：%s" % err}
        _save_disk_cache(rel)
    _cache["data"], _cache["at"] = rel, now
    return _wrap(current, rel, cached=False)


def _wrap(current: str, rel: dict, cached: bool) -> dict:
    latest = rel.get("version") or ""
    return {"ok": True, "current": current, "latest": latest, "tag": rel.get("tag"),
            "name": rel.get("name"), "notes": rel.get("notes") or "",
            "url": rel.get("url"), "published_at": rel.get("published_at") or "",
            "has_update": bool(latest) and is_newer(latest, current),
            "cached": cached, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def _explain(e: Exception) -> str:
    s = "%s: %s" % (type(e).__name__, e)
    low = s.lower()
    if "timed out" in low or "timeout" in low:
        return "连接 GitHub 超时（网络或代理问题），稍后再试"
    if "403" in low:
        return "GitHub 接口拒绝访问（可能查得太频繁，过几分钟再试）"
    if "404" in low:
        return "GitHub 上没有找到发布记录"
    if "getaddrinfo" in low or "name or service" in low:
        return "域名解析失败（本机 DNS / 代理没通）"
    return s


# ---------------- 更新 ----------------
def _zip_urls(tag: str) -> list:
    """tag 源码包的多线路地址：直连 + 几个常用加速前缀（和安装器一致）。"""
    path_tail = "panxian-AI/rvc-cover-station/zip/refs/tags/%s" % tag
    urls = ["https://codeload.github.com/" + path_tail]
    for p in ("ghfast.top", "ghproxy.net", "gh.llkk.cc", "gh-proxy.com",
              "gh.kkgithub.com", "ghproxy.cc", "ghproxy.link"):
        urls.append("https://%s/https://codeload.github.com/%s" % (p, path_tail))
    return urls


def _line_name(url: str) -> str:
    """线路的短名字，给界面/日志看（完整 URL 太长）。"""
    try:
        host = urllib.parse.urlparse(url).hostname or url
    except Exception:
        host = url
    if host.endswith("codeload.github.com"):
        return "GitHub 直连"
    return host


_last_download = {"url": "", "lines": [], "speed": 0.0}


def _download(tag: str) -> tuple[bytes, str]:
    """先给所有线路测速，再按实测速度从上到下试。

    为什么不是「按固定顺序逐个试」：加速镜像有的快有的半死，固定顺序要等到
    超时（每条 60 秒）才换下一条，用户体感就是「点了更新一直转圈」。这里先花
    一两秒并行探 128KB，直接挑最快的，失败再降级到次快的。
    """
    urls = _zip_urls(tag)
    cands = [{"label": _line_name(u), "url": u} for u in urls]
    ranked = []
    try:
        ranked = speed.rank(cands, nbytes=128 * 1024, timeout=6, workers=4)
    except Exception:
        ranked = []
    order = ranked or cands
    _last_download["lines"] = [
        {"label": c["label"], "ok": bool(c.get("ok")), "kbps": c.get("kbps") or 0,
         "ms": c.get("ms") or 0} for c in order]

    last = ""
    for i, c in enumerate(order):
        u = c["url"]
        try:
            data = _get(u, timeout=90)
            if len(data) < 1024 or data[:2] != b"PK":
                last = "返回内容不是 zip"
                continue
            if len(data) > MAX_ZIP:
                last = "更新包过大（%d 字节），已放弃" % len(data)
                continue
            _last_download["url"] = u
            _last_download["speed"] = c.get("kbps") or 0
            _last_download["tries"] = i + 1
            return data, u
        except Exception as e:
            last = _explain(e)
    raise RuntimeError("下载更新包失败：%s" % (last or "所有线路都不通"))


def _skippable(rel: str) -> bool:
    parts = [p.lower() for p in rel.replace("\\", "/").split("/") if p]
    if any(p in SKIP_PARTS for p in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if name in SKIP_NAMES or name.lower().endswith(SKIP_EXT):
        return True
    return False


def apply() -> dict:
    """下载最新 tag 的源码并覆盖代码目录。返回 ok/msg/明细。"""
    info = check(force=True)
    if not info.get("ok"):
        return {"ok": False, "msg": info.get("error") or "检测失败，先确认能连上 GitHub"}
    tag = info.get("tag") or ""
    new_ver = info.get("latest") or ""
    if not info.get("has_update"):
        return {"ok": False, "msg": "已经是最新版（%s），不需要更新" % info.get("current")}

    try:
        data, used = _download(tag)
    except Exception as e:
        return {"ok": False, "msg": str(e)}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, stamp)
    work = tempfile.mkdtemp(prefix="rvc_update_")
    written, backed, skipped = 0, 0, 0
    try:
        zpath = os.path.join(work, "src.zip")
        with open(zpath, "wb") as f:
            f.write(data)
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            root = names[0].split("/")[0] if names else ""
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                rel = zi.filename[len(root) + 1:] if root and zi.filename.startswith(root + "/") else zi.filename
                rel = rel.replace("\\", "/")
                top = rel.split("/")[0]
                target_root = next((dst for src, dst in DIR_MAP if src == top), None)
                if not target_root or rel == top:
                    continue
                sub = rel[len(top) + 1:]
                if not sub or _skippable(sub):
                    skipped += 1
                    continue
                dest = os.path.realpath(os.path.join(target_root, sub))
                # 防 zip 里带 ../ 写到目录外面
                if os.path.commonpath([dest, os.path.realpath(target_root)]) != os.path.realpath(target_root):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.isfile(dest) and os.path.getsize(dest) < 2 * 1024 * 1024:
                    bdst = os.path.join(backup, top, sub)
                    try:
                        os.makedirs(os.path.dirname(bdst), exist_ok=True)
                        shutil.copy2(dest, bdst)
                        backed += 1
                    except OSError:
                        pass
                with z.open(zi) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                written += 1
        # 版本号跟着更新，下次打开就不会再提示同一版
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(new_ver + "\n")
    except Exception as e:
        return {"ok": False, "msg": "更新失败：%s: %s" % (type(e).__name__, e),
                "backup": backup if backed else ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)

    _cache["at"] = 0.0
    return {"ok": True, "from": info.get("current"), "to": new_ver,
            "written": written, "backed_up": backed, "skipped": skipped,
            "backup": backup if backed else "", "source": used,
            "lines": _last_download.get("lines") or [],
            "speed_kbps": _last_download.get("speed") or 0,
            "tries": _last_download.get("tries") or 1,
            "msg": "已更新到 %s（替换 %d 个文件）。重启面板生效："
                   "关掉这个窗口，重新双击 启动RVC.bat" % (new_ver, written)}
