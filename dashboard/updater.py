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
    · **下完先核 SHA-256**：期望值取自 Release 正文（api.github.com，HTTPS，镜像改不了），
      和实际字节对不上就换线路，所有线路都对不上就拒装 —— 否则一个快的恶意镜像
      就能给所有已装机器下发任意代码（安装器里那 7 个加速前缀不受我们控制）；
    · 失败一律返回 {ok: False, msg: 中文原因}，绝不抛异常到路由层。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from paths import DATA, DASH, PROJ, RVC_ROOT

import speed

REPO = "panxian-AI/rvc-cover-station"
FALLBACK_VERSION = "2.1.3"          # 没有 VERSION 文件时（老安装）显示这个
VERSION_FILE = os.path.join(PROJ, "VERSION")
CACHE_FILE = os.path.join(DASH, "update_cache.json")
BACKUP_DIR = os.path.join(PROJ, "_update_backup")

CACHE_TTL = 600                      # 秒：同一个结果 10 分钟内不重复查
HTTP_TIMEOUT = 12
MAX_ZIP = 220 * 1024 * 1024          # 更新包大小上限，防被塞大文件

# 发布页没公布校验值时是否放行。默认不放行（fail closed）：
# 唯一能证明「这个包是我发的那一个」的东西就是正文里的 SHA-256，
# 没有它就等于把安装器的 7 个第三方加速前缀当成可信源。
ALLOW_UNVERIFIED = os.environ.get("RVC_ALLOW_UNVERIFIED_UPDATE", "") == "1"

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

# 根级文件：安装布局里它们也放在 PROJ 下。
# requirements.txt 尤其重要——少了它 deps.py 找不到清单，「依赖锁」就是空转的。
ROOT_FILES = {"requirements.txt", "install.json"}

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
    """版本号归一成可比较的四段元组。

    以前只取前三段：发 2.0.1.5 时老机器（2.0.1）永远收不到更新提示
    （两者比较结果相等），而这个 bug 没有任何报错。
    """
    parts = []
    for p in str(v or "").strip().lstrip("vV").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


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
    body = d.get("body") or ""
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "name": d.get("name") or tag,
        "notes": body,
        # 更新包的期望哈希：只能从正文来。api.github.com 是 HTTPS，镜像动不了；
        # 而正文是随 Release 一起创建的，改它需要仓库写权限。
        "sha256": _pick_sha(body),
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
                                            "published_at", "asset", "asset_name", "sha256")}
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
    sha = _norm_sha(rel.get("sha256"))
    return {"ok": True, "current": current, "latest": latest, "tag": rel.get("tag"),
            "name": rel.get("name"), "notes": rel.get("notes") or "",
            "url": rel.get("url"), "published_at": rel.get("published_at") or "",
            "sha256": sha,
            # verified = 发布页公布了校验值，这次更新会逐字节核对；
            # can_auto_update = 能不能点「立即更新」（没校验值且没显式放行就不让点）
            "verified": bool(sha),
            "can_auto_update": bool(sha) or ALLOW_UNVERIFIED,
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


# ---------------- 校验值 ----------------
# 发布正文里的锚点，两种写法都认（英文给机器看，中文给人看）
_SHA_IN_BODY = re.compile(
    r"(?:update[ _-]?zip[ _-]?sha-?256|更新包\s*sha-?256)[^0-9a-fA-F]{0,24}([0-9a-fA-F]{64})",
    re.I)


def _norm_sha(s) -> str:
    """把可能带前缀 / 空格 / 大写的哈希归一成 64 位小写；不像哈希就返回 ""。"""
    t = str(s or "").strip().lower()
    t = re.sub(r"^sha-?256\s*:\s*", "", t)
    t = re.sub(r"^0x", "", t)
    t = re.sub(r"[\s:_-]", "", t)
    return t if re.fullmatch(r"[0-9a-f]{64}", t) else ""


def _pick_sha(text) -> str:
    """从 Release 正文里取更新包校验值，取不到返回 ""。"""
    m = _SHA_IN_BODY.search(str(text or ""))
    return _norm_sha(m.group(1)) if m else ""


def _sha_reason(data: bytes, expect: str) -> str:
    """比对实际字节与期望值。通过返回 ""，不通过返回中文原因。"""
    want = _norm_sha(expect)
    if not want:
        return "没有可比对的校验值"
    got = hashlib.sha256(data).hexdigest()
    if got == want:
        return ""
    return "SHA-256 不一致（期望 %s…，实际 %s…）" % (want[:12], got[:12])


def _zip_broken(data: bytes) -> str:
    """整包 CRC 校验。返回出问题的条目名或异常描述，全好返回 ""。

    为什么必须做：apply() 是边解压边覆盖，包里有一处坏，前面几十个文件已经写进
    去了，走到坏的那一条才抛异常——留下一半新一半旧的代码，还得靠 _update_backup
    手工恢复。这里先验一遍，坏包直接换线路。

    注意：CRC 只防「传输损坏」，不防「有人故意换包」——篡改过的包自己也带着
    算得过的 CRC。防篡改靠 _sha_reason()，两者都要过。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
        return bad or ""
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def _download(tag: str, expect: str = "") -> tuple[bytes, str]:
    """先给所有线路测速，再按实测速度从上到下试；每条都要过 CRC 与 SHA-256。

    为什么不是「按固定顺序逐个试」：加速镜像有的快有的半死，固定顺序要等到
    超时（每条 60 秒）才换下一条，用户体感就是「点了更新一直转圈」。这里先花
    一两秒并行探 128KB，直接挑最快的，失败再降级到次快的。

    为什么速度快的也要先验哈希：测速排名意味着「谁快听谁的」，而加速前缀
    是第三方服务。只靠 CRC 的话，控制了任一镜像的人把包换掉，CRC 照样过，
    于是能发给所有已装机器。期望值来自 Release 正文（HTTPS API），镜像改不了。
    """
    want = _norm_sha(expect)
    if not want and not ALLOW_UNVERIFIED:
        raise RuntimeError(
            "发布页没有公布更新包的 SHA-256 校验值，无法确认下载到的是官方包，"
            "已拒绝自动更新。请到 Release 页面手动下载完整安装包安装。")

    urls = _zip_urls(tag)
    cands = [{"label": _line_name(u), "url": u} for u in urls]
    ranked = []
    try:
        ranked = speed.rank(cands, nbytes=128 * 1024, timeout=6, workers=4)
    except Exception:
        ranked = []
    order = ranked or cands
    lines = [{"label": c["label"], "ok": bool(c.get("ok")), "kbps": c.get("kbps") or 0,
              "ms": c.get("ms") or 0, "why": ""} for c in order]
    _last_download["lines"] = lines

    last = ""
    mismatch = 0
    for i, c in enumerate(order):
        u = c["url"]
        try:
            data = _get(u, timeout=90)
            if len(data) < 1024 or data[:2] != b"PK":
                last = lines[i]["why"] = "返回内容不是 zip"
                continue
            if len(data) > MAX_ZIP:
                last = lines[i]["why"] = "包过大（%d 字节）" % len(data)
                continue
            broken = _zip_broken(data)
            if broken:
                last = lines[i]["why"] = "包不完整（%s）" % broken
                continue
            bad = _sha_reason(data, want) if want else ""
            if bad:
                mismatch += 1
                last = lines[i]["why"] = "校验失败：" + bad
                continue
            _last_download["url"] = u
            _last_download["speed"] = c.get("kbps") or 0
            _last_download["tries"] = i + 1
            _last_download["sha256"] = hashlib.sha256(data).hexdigest()
            lines[i]["why"] = "校验通过"
            return data, u
        except Exception as e:
            last = lines[i]["why"] = _explain(e)

    if mismatch and mismatch == len(order):
        raise RuntimeError(
            "所有线路给出的更新包都与发布页公布的 SHA-256 对不上，已拒绝安装。"
            "这通常意味着某个加速镜像返回了被替换的内容；"
            "请到 Release 页面手动下载完整安装包安装。")
    raise RuntimeError("下载更新包失败：%s" % (last or "所有线路都不通"))


def _skippable(rel: str) -> bool:
    parts = [p.lower() for p in rel.replace("\\", "/").split("/") if p]
    if any(p in SKIP_PARTS for p in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if name in SKIP_NAMES or name.lower().endswith(SKIP_EXT):
        return True
    return False


_APPLY_LOCK = threading.Lock()     # 连点两次「更新」不许并行写同一批文件


def _stage_file(z, zi, dest):
    """把一个成员写成「临时文件 → os.replace」，绝不在原文件上直接截断。

    直接 open(dest, "wb") 会先把原文件清零：磁盘满 / 被 AV 占用 / 写到一半
    断电，就留下一个 0 字节或半截的 .py，整个面板都起不来，而且已覆盖的
    文件不会回滚。临时文件 + 原子替换至少保证「要么旧、要么新」。
    """
    tmp = dest + ".rvcnew"
    try:
        with z.open(zi) as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _rollback(touched):
    """把这次更新动过的文件还原（bak 存在就写回，原本不存在就删掉）。"""
    n = 0
    for dest, bak, existed in reversed(touched):
        try:
            if bak and os.path.isfile(bak):
                shutil.copy2(bak, dest)
                n += 1
            elif not existed and os.path.isfile(dest):
                os.remove(dest)
                n += 1
        except OSError:
            pass
    return n


def apply() -> dict:
    """下载最新 tag 的源码并覆盖代码目录。返回 ok/msg/明细。"""
    with _APPLY_LOCK:
        return _apply_locked()


def _apply_locked() -> dict:
    info = check(force=True)
    if not info.get("ok"):
        return {"ok": False, "msg": info.get("error") or "检测失败，先确认能连上 GitHub"}
    tag = info.get("tag") or ""
    new_ver = info.get("latest") or ""
    if not info.get("has_update"):
        return {"ok": False, "msg": "已经是最新版（%s），不需要更新" % info.get("current")}

    expect = _norm_sha(info.get("sha256"))
    if not expect and not ALLOW_UNVERIFIED:
        return {"ok": False, "msg": "发布页没有公布更新包的 SHA-256 校验值，"
                                    "没法确认下载到的是官方包，已拒绝自动更新。"
                                    "请到 Release 页面手动下载完整安装包安装。"}

    try:
        data, used = _download(tag, expect)
    except Exception as e:
        return {"ok": False, "msg": str(e)}

    # 落盘前再核一次。_download 已经验过，这里是为了「以后有人重构
    # _download、把校验弄丢了」也不至于直接写坏盘——解压是会覆盖文件的。
    if expect:
        bad = _sha_reason(data, expect)
        if bad:
            return {"ok": False, "msg": "更新包校验未通过（%s），已拒绝写入磁盘。" % bad}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, stamp)
    work = tempfile.mkdtemp(prefix="rvc_update_")
    written, backed, skipped = 0, 0, 0
    touched = []          # [(dest, 备份路径 or None, 写之前是否存在)]
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

                # 根级文件（requirements.txt / install.json）：直接落到 PROJ 下。
                # 以前这里被 `rel == top` 一律跳过，于是依赖锁清单永远不会被更新。
                if rel in ROOT_FILES:
                    dest = os.path.realpath(os.path.join(PROJ, rel))
                    if os.path.dirname(dest) != os.path.realpath(PROJ):
                        skipped += 1
                        continue
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    existed = os.path.isfile(dest)
                    bdst = None
                    if existed and os.path.getsize(dest) < 2 * 1024 * 1024:
                        bdst = os.path.join(backup, "_root", rel)
                        try:
                            os.makedirs(os.path.dirname(bdst), exist_ok=True)
                            shutil.copy2(dest, bdst)
                            backed += 1
                        except OSError:
                            bdst = None
                    touched.append((dest, bdst, existed))
                    _stage_file(z, zi, dest)
                    written += 1
                    continue

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
                existed = os.path.isfile(dest)
                bdst = None
                if existed and os.path.getsize(dest) < 2 * 1024 * 1024:
                    bdst = os.path.join(backup, top, sub)
                    try:
                        os.makedirs(os.path.dirname(bdst), exist_ok=True)
                        shutil.copy2(dest, bdst)
                        backed += 1
                    except OSError:
                        bdst = None
                touched.append((dest, bdst, existed))
                _stage_file(z, zi, dest)
                written += 1
        # 版本号跟着更新，下次打开就不会再提示同一版（同样走原子写）
        vz = os.path.join(work, "VERSION")
        with open(vz, "w", encoding="utf-8") as f:
            f.write(new_ver + "\n")
        os.replace(vz, VERSION_FILE)
    except Exception as e:
        # 已经写进去的文件必须还原，否则会留下「一半新一半旧」的代码目录
        restored = _rollback(touched)
        return {"ok": False, "msg": "更新失败：%s: %s（已还原 %d 个文件）"
                                    % (type(e).__name__, e, restored),
                "backup": backup if backed else "", "rolled_back": restored}
    finally:
        shutil.rmtree(work, ignore_errors=True)

    _cache["at"] = 0.0
    return {"ok": True, "from": info.get("current"), "to": new_ver,
            "written": written, "backed_up": backed, "skipped": skipped,
            "backup": backup if backed else "", "source": used,
            "sha256": expect or _last_download.get("sha256") or "",
            "verified": bool(expect),
            "lines": _last_download.get("lines") or [],
            "speed_kbps": _last_download.get("speed") or 0,
            "tries": _last_download.get("tries") or 1,
            "msg": "已更新到 %s（替换 %d 个文件，已核对 SHA-256）。重启面板生效："
                   "关掉这个窗口，重新双击 启动RVC.bat" % (new_ver, written)}
