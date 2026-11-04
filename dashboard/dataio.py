# -*- coding: utf-8 -*-
"""数据备份与迁移：导出 / 导入。

为什么要有它：
    站长手上最值钱的是几百条访客申请和一堆白名单——这些只存在
    dashboard\\guest_db.json 一个文件里。换机器、重装系统、手改 JSON 改坏了，
    没有第二条路可走。这里给一条**可验证**的路：导出一份带 sha256 的 zip，
    随时导回来。

导出内容（按需勾选）：
    data/guest_db.json           访客 / 申请 / 队列 / 白名单（必含）
    data/advanced_settings.json  高级设置
    data/covers_index.json       成品库索引
    covers/*.mp3                 成品音频（体积大，默认不含）
    models/manifest.json         已装声线清单（只记名字与大小，换机后据此核对缺哪个）
    manifest.json                版本、时间、条目大小与 sha256、各项条数

设计取舍：
    · zip 里的条目名是固定的 ASCII 白名单，**从不按 manifest 里的路径解压**，
      所以拿一个恶意 zip 过来也写不出 COVERS_DIR 以外的地方（没有 zip slip 面）；
    · 导入分两步：check 只读不写（列出「会变成什么样」），apply 才落盘；
      apply 前先把现有数据整体备份一份，任何一步失败都从这份备份整体还原；
    · 合并模式按业务主键去重（visits/requests 用 id，whitelist 用 gid），
      不是简单拼接——否则「同一个包导两次」就会把每条申请复制成两条。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
import threading
import time
import zipfile

from paths import COVERS_DIR, DATA, PROJ, RVC_ROOT

HERE = os.path.dirname(os.path.abspath(__file__))

APP = "rvc-cover-station"
KIND = "data-backup"
SCHEMA = 1                      # 包格式版本；将来改结构就 +1，老包仍按老规则读

# 备份落在 RVCSVC\\_backup（和代码同盘，升级代码不会覆盖它）
BACKUP_DIR = os.environ.get("RVC_BACKUP_DIR") or os.path.join(PROJ, "_backup")

MAX_ZIP = 4 * 1024 * 1024 * 1024     # 单个包上限 4GB（比这大必然是选错文件了）
MAX_MEMBER = 1024 * 1024 * 1024      # 单个成员解压后上限 1GB（成品 mp3 远小于这个）
MAX_UNPACK = 6 * 1024 * 1024 * 1024  # 整包解压后上限 6GB（防高压缩比的解压炸弹）
MAX_KEEP_AUTO = 20                  # 「导入前自动备份」最多留 20 份
MAX_KEEP_MANUAL = 50                # 手动导出最多留 50 份（再多就提示用户删）

AUDIO_PREFIX = "covers/"
DATA_PREFIX = "data/"

# 只认这几个固定条目名。凡是 manifest 里出现别的一律拒绝。
SAFE_ARCS = {
    "data/guest_db.json",
    "data/advanced_settings.json",
    "data/covers_index.json",
    "models/manifest.json",
}

_LOCK = threading.RLock()
_LAST_EXPORT = {"path": None, "token": None, "ts": 0.0}


# ---------------- 延迟取模块（避免和 panel 的导入顺序打架） ----------------
def _guest():
    try:
        import guest
        return guest
    except Exception:
        return None


def _settings():
    try:
        import settings
        return settings
    except Exception:
        return None


def _cover():
    try:
        import cover
        return cover
    except Exception:
        return None


def _covers_dir():
    # 测试实例用 RVC_COVERS_DIR 指到临时目录，别碰真实成品库
    return os.environ.get("RVC_COVERS_DIR") or COVERS_DIR


def _db_path():
    g = _guest()
    p = getattr(g, "DB_PATH", None) if g else None
    return p or os.environ.get("RVC_GUEST_DB") or os.path.join(HERE, "guest_db.json")


def _settings_path():
    s = _settings()
    p = getattr(s, "PATH", None) if s else None
    return p or os.environ.get("RVC_SETTINGS_PATH") or os.path.join(HERE, "advanced_settings.json")


def _index_path():
    return os.path.join(_covers_dir(), "index.json")


def version():
    try:
        with open(os.path.join(PROJ, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


# ---------------- 小工具 ----------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path, chunk=1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _loads(raw, default=None):
    try:
        obj = json.loads(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
        return obj if isinstance(obj, dict) else default
    except Exception:
        return default


def _dump(obj) -> bytes:
    # 和 guest._save 保持同一种排版（indent=1 / 不转义中文），差异看得很清楚
    return json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")


def _human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def _tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _counts(db) -> dict:
    db = db if isinstance(db, dict) else {}
    reqs = db.get("requests") or []
    by = {"pending": 0, "queued": 0, "running": 0, "done": 0,
          "rejected": 0, "error": 0}
    for r in reqs:
        s = str((r or {}).get("state") or "")
        if s in by:
            by[s] += 1
    return {
        "visits": len(db.get("visits") or []),
        "requests": len(reqs),
        "queue": len(db.get("queue") or []),
        "wl_apps": len(db.get("wl_apps") or []),
        "whitelist": len(db.get("whitelist") or []),
        "pending": by["pending"], "queued": by["queued"], "running": by["running"],
        "done": by["done"], "rejected": by["rejected"], "error": by["error"],
    }


def _cover_counts() -> dict:
    p = _index_path()
    n, total = 0, 0
    try:
        arr = json.loads(_read_bytes(p).decode("utf-8", "ignore"))
        if isinstance(arr, list):
            n = len(arr)
            total = sum(int(i.get("size") or 0) for i in arr if isinstance(i, dict))
    except Exception:
        pass
    files = 0
    try:
        files = len([f for f in os.listdir(_covers_dir()) if f.lower().endswith(".mp3")])
    except OSError:
        pass
    return {"indexed": n, "files": files, "bytes": total}


def _models_manifest() -> dict:
    """已装声线清单：只记名字/大小/时间，不含权重本身（.pth 动辄几百 MB）。"""
    out = {"weights": [], "indices": []}
    for key, sub in (("weights", "weights"), ("indices", "indices")):
        d = os.path.join(RVC_ROOT, "assets", sub)
        try:
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                if not os.path.isfile(p):
                    continue
                if sub == "weights" and not name.lower().endswith(".pth"):
                    continue
                if sub == "indices" and not name.lower().endswith(".index"):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[key].append({"name": name, "bytes": st.st_size,
                                 "mtime": int(st.st_mtime)})
        except OSError:
            pass
    return out


# ---------------- 备份目录 ----------------
def ensure_dir() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def _new_path(kind="manual") -> str:
    ensure_dir()
    prefix = "导入前自动备份" if kind == "auto" else "RVC数据备份"
    base = "%s-%s" % (prefix, _tag())
    p = os.path.join(BACKUP_DIR, base + ".zip")
    i = 2
    while os.path.exists(p):
        p = os.path.join(BACKUP_DIR, "%s-%d.zip" % (base, i))
        i += 1
    return p


def list_backups(limit=40) -> list:
    """列备份（新的在前）。时间从文件名解析，不读包内容——列表要快。"""
    out = []
    try:
        names = os.listdir(BACKUP_DIR)
    except OSError:
        return out
    for n in names:
        if not n.lower().endswith(".zip"):
            continue
        p = os.path.join(BACKUP_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        auto = n.startswith("导入前自动备份")
        out.append({"name": n, "bytes": st.st_size, "size_text": _human(st.st_size),
                    "mtime": int(st.st_mtime), "auto": auto,
                    "kind": "auto" if auto else "manual"})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def _prune() -> list:
    """按份数裁剪。自动备份留得少（用户看不见，容易堆）；手动导出的留得多
    （那是用户自己要留的），但也不能无限涨——50 份 × 二十来 KB 才 1MB 出头。"""
    items = list_backups(limit=10000)
    gone = []
    for is_auto, cap in ((True, MAX_KEEP_AUTO), (False, MAX_KEEP_MANUAL)):
        grp = [x for x in items if bool(x["auto"]) is is_auto]
        for x in grp[cap:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, x["name"]))
                gone.append(x["name"])
            except OSError:
                pass
    return gone


def delete_backup(name) -> dict:
    """删除一份备份。只认备份目录下的 .zip 基名，防止传 ../ 删别的文件。"""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    if not base.lower().endswith(".zip") or base != str(name or "").replace("\\", "/"):
        return {"ok": False, "msg": "文件名不合法"}
    p = os.path.join(BACKUP_DIR, base)
    if not os.path.isfile(p):
        return {"ok": False, "msg": "这份备份已经不在了"}
    try:
        os.remove(p)
    except OSError as e:
        return {"ok": False, "msg": "删除失败：%s" % e}
    return {"ok": True, "msg": "已删除 %s" % base, "name": base}


def _safe_backup_path(path) -> str | None:
    """导入时只接受「本机真实存在的 zip」，且不能是目录/设备文件。"""
    p = os.path.abspath(str(path or "").strip().strip('"'))
    if not p or not os.path.isfile(p):
        return None
    if not p.lower().endswith(".zip"):
        return None
    try:
        if os.path.getsize(p) <= 0 or os.path.getsize(p) > MAX_ZIP:
            return None
    except OSError:
        return None
    return p


def _cur_db():
    """本机「现在」的访客库：优先取 guest 的内存快照，而不是读文件。

    为什么：soft 写被节流的 2 秒里，文件比内存旧。按文件算预览/合并会少算刚
    发生的申请，合并时还可能把本机最新的一条判成「更旧的那份」。
    """
    g = _guest()
    if g and hasattr(g, "snapshot"):
        try:
            return g.snapshot()
        except Exception:
            pass
    return _loads(_read_bytes(_db_path())) if os.path.isfile(_db_path()) else None


def _replace_guest(new_db) -> bool:
    """把整份库交给 guest 写入（它有自己的内存副本和锁）。

    直接写文件是错的：面板运行中 guest 内存里还有一份，任何一次 _save() 都会把
    旧内容盖回去，导入静默丢失。没有 guest 模块时（离线修数据）才退回写文件。
    """
    g = _guest()
    if g and hasattr(g, "replace_db"):
        try:
            g.replace_db(new_db)
            return True
        except Exception:
            pass
    return False


# ---------------- 当前概况 ----------------
def summary() -> dict:
    """控制台「数据备份」卡片顶部那一行 + 备份列表。"""
    db = _cur_db()
    st = _loads(_read_bytes(_settings_path())) if os.path.isfile(_settings_path()) else None
    bks = list_backups()
    return {
        "ok": True,
        "dir": BACKUP_DIR,
        "version": version(),
        "machine": socket.gethostname(),
        "counts": _counts(db or {}),
        "covers": _cover_counts(),
        "settings": bool(st),
        "has_db": os.path.isfile(_db_path()),
        "db_bytes": (os.path.getsize(_db_path()) if os.path.isfile(_db_path()) else 0),
        "backups": bks,
        "total_bytes": sum(x["bytes"] for x in bks),
        "total_text": _human(sum(x["bytes"] for x in bks)),
        "limits": {"auto": MAX_KEEP_AUTO, "manual": MAX_KEEP_MANUAL},
    }


# ---------------- 导出 ----------------
def _collect(with_audio: bool) -> tuple[list, list, dict]:
    """读齐要打包的 JSON 条目（音频只列名字，稍后流式写）。"""
    items, skipped, warns = [], [], []
    g = _guest()
    if g and hasattr(g, "flush"):
        try:
            g.flush()          # 把被节流压住的 soft 写补上，别导出个旧库
        except Exception as e:
            warns.append("访客库 flush 失败：%s" % e)

    plan = [
        ("guest_db", _db_path(), "data/guest_db.json", "访客与申请数据", True),
        ("settings", _settings_path(), "data/advanced_settings.json", "高级设置", False),
        ("covers_index", _index_path(), "data/covers_index.json", "成品库索引", False),
    ]
    for key, src, arc, label, required in plan:
        if not os.path.isfile(src):
            skipped.append({"key": key, "label": label, "why": "本机还没有这份数据"})
            continue
        try:
            raw = _read_bytes(src)
        except OSError as e:
            if required:
                raise RuntimeError("读不到 %s：%s" % (label, e))
            skipped.append({"key": key, "label": label, "why": "读取失败：%s" % e})
            continue
        obj = None
        bad = False
        if key == "guest_db":
            obj = _loads(raw)
            bad = obj is None
        elif key == "covers_index":
            # 成品索引是数组，和 guest_db 的对象结构不同，不能共用 _loads
            try:
                obj = json.loads(raw.decode("utf-8", "ignore"))
                bad = not isinstance(obj, list)
            except Exception:
                bad = True
        if bad:
            # 库文件坏了不该让整个导出失败，但必须说清楚，别让人以为备份是好的
            skipped.append({"key": key, "label": label,
                            "why": "文件内容不是合法 JSON（本机这份可能已损坏）"})
            continue
        it = {"key": key, "arc": arc, "label": label, "data": raw,
              "bytes": len(raw), "sha256": _sha(raw)}
        if key == "guest_db":
            it["counts"] = _counts(obj)
        elif key == "covers_index":
            it["counts"] = {"covers": len(obj)}
        items.append(it)

    # 声线清单：不是一个真实文件，是现场扫出来的
    try:
        man = _models_manifest()
        raw = _dump(man)
        items.append({"key": "models", "arc": "models/manifest.json",
                      "label": "已装声线清单", "data": raw, "bytes": len(raw),
                      "sha256": _sha(raw),
                      "counts": {"weights": len(man["weights"]),
                                 "indices": len(man["indices"])}})
    except Exception as e:
        skipped.append({"key": "models", "label": "已装声线清单", "why": str(e)})

    audio = []
    if with_audio:
        try:
            audio = sorted(f for f in os.listdir(_covers_dir())
                           if f.lower().endswith(".mp3"))
        except OSError:
            warns.append("成品目录读不到，这次没有打包音频")
    return items, audio, {"skipped": skipped, "warns": warns}


def _audio_arc(name) -> str:
    # 只取基名：目录里万一有奇怪字符也写不出 covers/ 之外
    return AUDIO_PREFIX + os.path.basename(str(name or ""))


def export(with_audio=False, kind="manual") -> dict:
    """打包一份备份，返回 {ok, path, name, bytes, items, counts}。

    kind="auto" 用于导入前的自动存档（按份数裁剪，用户看不到就别删太多）。
    """
    t0 = time.time()
    with _LOCK:
        try:
            items, audio, extra = _collect(bool(with_audio))
        except Exception as e:
            return {"ok": False, "msg": "打包失败：%s: %s" % (type(e).__name__, e)}
        # 只有一个空的访客库 + 声线清单时不该让人以为「备份好了」——
        # 那种包导到哪台机器上都恢复不出任何记录，不如直说没东西可备。
        real = []
        for it in items:
            if it["arc"] == "models/manifest.json":
                continue
            if it["arc"] == "data/guest_db.json":
                c = it.get("counts") or {}
                if any(c.get(k) for k in ("visits", "requests", "queue",
                                          "wl_apps", "whitelist")):
                    real.append(it)
            else:
                real.append(it)
        if not real:
            return {"ok": False, "empty": True,
                    "msg": "本机还没有可导出的数据（访客库是空的，也没有设置和成品）"}
        dst = _new_path(kind)
        tmp = dst + ".part"
        written = []
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for it in items:
                    zf.writestr(it["arc"], it["data"])
                    written.append({"arc": it["arc"], "bytes": it["bytes"],
                                    "sha256": it["sha256"], "label": it["label"],
                                    "counts": it.get("counts")})
                for name in audio:
                    src = os.path.join(_covers_dir(), name)
                    try:
                        sz = os.path.getsize(src)
                    except OSError:
                        continue
                    # mp3 已经压过，再 deflate 只是白烧 CPU
                    zf.write(src, _audio_arc(name), compress_type=zipfile.ZIP_STORED)
                    written.append({"arc": _audio_arc(name), "bytes": sz,
                                    "sha256": "", "label": name, "fast": True})
                db_counts = next((i.get("counts") for i in items if i["key"] == "guest_db"), {})
                man = {
                    "app": APP, "kind": KIND, "schema": SCHEMA,
                    "version": version(),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "created_ts": int(time.time()),
                    "machine": socket.gethostname(),
                    "counts": db_counts or {},
                    "covers": _cover_counts(),
                    "with_audio": bool(audio),
                    "audio_files": len(audio),
                    "files": written,
                    "skipped": extra["skipped"],
                }
                zf.writestr("manifest.json", _dump(man))
            os.replace(tmp, dst)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"ok": False, "msg": "打包失败：%s: %s" % (type(e).__name__, e)}

        size = os.path.getsize(dst)
        gone = _prune()
        _LAST_EXPORT.update({"path": dst, "ts": time.time()})
        return {"ok": True, "path": dst, "name": os.path.basename(dst),
                "bytes": size, "size_text": _human(size),
                "items": [{k: v for k, v in it.items() if k != "data"} for it in items],
                "audio_files": len(audio),
                "counts": next((i.get("counts") for i in items if i["key"] == "guest_db"), {}),
                "skipped": extra["skipped"], "warns": extra["warns"],
                "pruned": gone, "secs": round(time.time() - t0, 1),
                "dir": BACKUP_DIR,
                "msg": "已导出 %s（%s）" % (os.path.basename(dst), _human(size))}


# ---------------- 校验 ----------------
def _arc_ok(arc) -> bool:
    """条目名白名单：data/ 下三个固定文件、models/manifest.json、covers/ 一层 mp3。"""
    s = str(arc or "")
    if s in SAFE_ARCS:
        return True
    if not s.startswith(AUDIO_PREFIX):
        return False
    name = s[len(AUDIO_PREFIX):]
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return False
    if name != os.path.basename(name) or ".." in name:
        return False
    return name.lower().endswith(".mp3")


def check(zip_path) -> dict:
    """只读预检：包是不是本程序的、有没有被改过、导进来会变成什么样。"""
    p = _safe_backup_path(zip_path)
    if not p:
        return {"ok": False, "msg": "请选择一份本程序导出的 .zip 备份文件（4GB 以内）"}
    try:
        token = _sha_file(p)
        zf = zipfile.ZipFile(p)
    except zipfile.BadZipFile:
        return {"ok": False, "msg": "这个文件不是有效的 zip（可能没复制完整，或选错了文件）"}
    except OSError as e:
        return {"ok": False, "msg": "读不到这个文件：%s" % e}
    except Exception as e:
        return {"ok": False, "msg": "打开失败：%s: %s" % (type(e).__name__, e)}

    with zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            return {"ok": False, "msg": "包里没有 manifest.json —— 这不是本程序导出的数据备份"}
        try:
            man = _loads(zf.read("manifest.json"))
        except Exception as e:
            return {"ok": False, "msg": "manifest.json 读不出来：%s" % e}
        if not man:
            return {"ok": False, "msg": "manifest.json 内容不完整"}
        if man.get("app") != APP:
            return {"ok": False, "msg": "这不是 RVC 翻唱站的数据包（app=%s）" % man.get("app")}
        if man.get("kind") != KIND:
            return {"ok": False, "msg": "包类型不对（kind=%s）" % man.get("kind")}
        try:
            schema = int(man.get("schema") or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema < 1:
            return {"ok": False, "msg": "包格式版本缺失，来源可疑"}
        if schema > SCHEMA:
            return {"ok": False,
                    "msg": "这份备份来自更新版本的程序（包格式 v%d，本机只认到 v%d），"
                           "请先把程序升级到最新版再导入" % (schema, SCHEMA)}

        files = man.get("files")
        if not isinstance(files, list) or not files:
            return {"ok": False, "msg": "包里没有文件清单"}

        bad, items, audio_n, audio_bytes = [], [], 0, 0
        audio_arcs = []
        unpacked = 0
        for f in files:
            if not isinstance(f, dict):
                bad.append("清单里有一条不是对象")
                continue
            arc = str(f.get("arc") or "")
            if not _arc_ok(arc):
                # 这一条是硬拒绝：宁可整包不收，也不试着"挑能用的导"
                return {"ok": False, "msg": "包里含有不认识的路径（%s），已拒绝导入" % arc[:80]}
            if arc not in names:
                bad.append("%s 在包里不存在" % arc)
                continue
            # 解压炸弹：只卡压缩包体积没用，一个几百 MB 的全零 mp3 压缩后只有几十 KB，
            # 预检阶段就能把内存打满。先看声明的大小，超限直接拒收。
            try:
                fi = zf.getinfo(arc)
            except KeyError:
                bad.append("%s 在包里不存在" % arc)
                continue
            if fi.file_size > MAX_MEMBER:
                return {"ok": False, "msg": "包里有一项解压后过大（%s，%.1f MB），已拒绝导入"
                                            % (arc[:60], fi.file_size / 1048576.0)}
            unpacked += fi.file_size
            if unpacked > MAX_UNPACK:
                return {"ok": False, "msg": "包里内容解压后超过 %.1f GB，已拒绝导入"
                                            % (MAX_UNPACK / 1073741824.0)}
            try:
                raw = zf.read(arc)
            except Exception as e:
                bad.append("%s 读不出来（%s）" % (arc, e))
                continue
            want = int(f.get("bytes") or 0)
            if want and len(raw) != want:
                bad.append("%s 大小对不上（清单 %d，实际 %d）" % (arc, want, len(raw)))
                continue
            sha = str(f.get("sha256") or "")
            if sha and _sha(raw) != sha:
                bad.append("%s 内容被改动过（校验值不符）" % arc)
                continue
            if f.get("fast"):
                audio_n += 1
                audio_bytes += len(raw)
                audio_arcs.append(arc)
                continue
            items.append({"key": os.path.basename(arc), "arc": arc,
                          "label": str(f.get("label") or arc),
                          "bytes": len(raw), "counts": f.get("counts") or {},
                          "data": raw})

        if bad:
            return {"ok": False, "msg": "这份备份不完整，已拒绝导入：" + "；".join(bad[:3])}

        # 结构校验：能打开不等于能用
        db = None
        for it in items:
            if it["arc"] == "data/guest_db.json":
                db = _loads(it["data"])
                if db is None:
                    return {"ok": False, "msg": "备份里的访客库不是合法 JSON，已拒绝导入"}
        st = None
        for it in items:
            if it["arc"] == "data/advanced_settings.json":
                st = _loads(it["data"])
        idx = None
        for it in items:
            if it["arc"] == "data/covers_index.json":
                try:
                    idx = json.loads(it["data"].decode("utf-8", "ignore"))
                except Exception:
                    idx = None

    cur_db = _cur_db()
    raw_counts = _counts(db or {})

    # 两种导入方式各自会变成什么样：预览必须准，用户才敢点。
    # 用深拷贝算，别把「包里原本是什么状态」和「导入后会是什么状态」搅在一起。
    def _norm_copy(d):
        return _normalize(copy.deepcopy(d))[0]

    preview = {"replace": dict(raw_counts), "merge": _counts(cur_db or {})}
    merge_stat = None
    try:
        if db is not None:
            inc_n = _norm_copy(db)
            preview["replace"] = _counts(inc_n)
            if cur_db is not None:
                m, merge_stat = _merge_guest_db(copy.deepcopy(cur_db), inc_n)
                preview["merge"] = _counts(_norm_copy(m))
            else:
                preview["merge"] = dict(preview["replace"])
    except Exception:
        pass

    model_now = {m["name"] for m in _models_manifest()["weights"]}
    man_models = None
    for it in items:
        if it["arc"] == "models/manifest.json":
            man_models = _loads(it["data"])
    missing_models = []
    if man_models and isinstance(man_models.get("weights"), list):
        names = {str(x.get("name") or "") for x in man_models["weights"]}
        missing_models = sorted(n for n in names if n and n not in model_now)

    out_items = [{k: v for k, v in it.items() if k != "data"} for it in items]
    return {
        "ok": True,
        "token": token,
        "path": p,
        "name": os.path.basename(p),
        "bytes": os.path.getsize(p),
        "size_text": _human(os.path.getsize(p)),
        "version": str(man.get("version") or "?"),
        "created": str(man.get("created") or "?"),
        "machine": str(man.get("machine") or "?"),
        "schema": schema,
        "items": out_items,
        "counts": raw_counts,
        "current": _counts(cur_db or {}),
        "preview": preview,
        "merge_stat": merge_stat,
        "covers": {"now": _cover_counts()["indexed"],
                   "incoming": (len(idx) if isinstance(idx, list) else 0)},
        "settings_included": st is not None,
        "audio_files": audio_n,
        "audio_bytes": audio_bytes,
        "audio_arcs": audio_arcs,
        "audio_text": _human(audio_bytes),
        "missing_models": missing_models[:20],
        "missing_models_n": len(missing_models),
        "msg": "备份有效：%s 导出的 %s（%s）" % (man.get("machine") or "?", 
                                                man.get("created") or "?", _human(os.path.getsize(p))),
    }


# ---------------- 合并 / 整理 ----------------
def _by_id(arr) -> dict:
    out = {}
    for x in arr or []:
        if isinstance(x, dict) and x.get("id"):
            out[str(x["id"])] = x
    return out


def _newer(a, b) -> bool:
    """a 是否比 b 更新：先看 decided（决定时间），再看 created。"""
    def t(x):
        try:
            return float(x.get("decided") or x.get("created") or 0)
        except (TypeError, ValueError):
            return 0.0
    return t(a) > t(b)


def _merge_guest_db(cur, inc) -> tuple[dict, dict]:
    """把导入的库合并进当前库。主键：visits/requests/wl_apps 用 id，白名单用 gid。"""
    cur = cur if isinstance(cur, dict) else {}
    inc = inc if isinstance(inc, dict) else {}
    out = {k: list(cur.get(k) or []) for k in ("visits", "requests", "wl_apps", "whitelist")}
    out["queue"] = [str(x) for x in (cur.get("queue") or [])]
    stat = {"visits_added": 0, "visits_updated": 0, "requests_added": 0,
            "requests_updated": 0, "wl_apps_added": 0, "wl_apps_updated": 0,
            "whitelist_added": 0, "whitelist_updated": 0}

    # 访客：同 id 取 last 更新的那条；count 取两边较大值（不是相加，否则每导一次就翻倍）
    cur_v = _by_id(out["visits"])
    order = [str(x.get("id")) for x in out["visits"] if isinstance(x, dict) and x.get("id")]
    for v in inc.get("visits") or []:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        k = str(v["id"])
        old = cur_v.get(k)
        if not old:
            out["visits"].append(v)
            cur_v[k] = v
            order.append(k)
            stat["visits_added"] += 1
        else:
            try:
                if float(v.get("last") or 0) > float(old.get("last") or 0):
                    old.update({kk: vv for kk, vv in v.items() if kk != "count"})
                    stat["visits_updated"] += 1
                old["count"] = max(int(old.get("count") or 0), int(v.get("count") or 0))
            except (TypeError, ValueError):
                pass

    for key, tag in (("requests", "requests"), ("wl_apps", "wl_apps")):
        cur_m = _by_id(out[key])
        order_k = [str(x.get("id")) for x in out[key] if isinstance(x, dict) and x.get("id")]
        for r in inc.get(key) or []:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            k = str(r["id"])
            old = cur_m.get(k)
            if not old:
                out[key].append(r)
                cur_m[k] = r
                order_k.append(k)
                stat[tag + "_added"] += 1
            elif old.get("state") == "done" and r.get("state") != "done":
                # 本机已经做完的单，不能被包里更「新」的 error/rejected 覆盖：
                # 那样成品下载链接会消失（mp3 还在 covers 里变成孤儿）
                continue
            elif _newer(r, old):
                old.update(r)
                stat[tag + "_updated"] += 1

    cur_w = {str(x.get("gid")): x for x in out["whitelist"]
             if isinstance(x, dict) and x.get("gid")}
    for w in inc.get("whitelist") or []:
        if not isinstance(w, dict) or not w.get("gid"):
            continue
        k = str(w["gid"])
        old = cur_w.get(k)
        if not old:
            out["whitelist"].append(w)
            cur_w[k] = w
            stat["whitelist_added"] += 1
        else:
            changed = False
            try:
                if float(w.get("last_seen") or 0) > float(old.get("last_seen") or 0):
                    old.update(w)
                    changed = True
            except (TypeError, ValueError):
                pass
            try:
                if int(w.get("hits") or 0) > int(old.get("hits") or 0):
                    old["hits"] = int(w.get("hits") or 0)
                    changed = True
            except (TypeError, ValueError):
                pass
            if w.get("approved") and not old.get("approved"):
                old["approved"] = True
                changed = True
            if changed:
                stat["whitelist_updated"] += 1

    return out, stat


def _job_alive(r) -> bool:
    """这条 running 记录对应的 job 在本进程里是否真的还在跑。

    查不动（模块没加载/接口变了）时按「还活着」处理：宁可让它下一轮再查，
    也不要重复开跑。查得到但返回 None = 确实没有这个 job（别的机器导进来的），
    才允许打回队列。
    """
    jid = str(r.get("job") or "")
    if not jid:
        return False
    try:
        mod = None
        if r.get("type") == "tts":
            import tts as mod
        else:
            import cover as mod
        return bool(mod.job_status(jid))
    except Exception:
        return True


def _normalize(db, prefer_queue=None) -> tuple[dict, dict]:
    """把「在别的机器上跑到一半」的状态收拾干净。

    为什么必须做：备份可能在制作中做的，那些 running 记录对应的 job 进程
    在目标机上根本不存在；原样导进来，队列会永远卡在一个跑不完的槽位上。
    统一改回 queued 让它重新排一次，访客还能等到成品。
    """
    fixed = 0
    for r in db.get("requests") or []:
        if not isinstance(r, dict):
            continue
        if r.get("state") == "running":
            # 本机还在跑的单（导入自己刚导出的备份时）不能打回队列：
            # 否则 pump() 会再开一单，同一个 rid 两个 watcher，先结束的那个
            # 会把已完成的结果改成失败。
            if _job_alive(r):
                continue
            r["state"] = "queued"
            r.pop("job", None)
            r.pop("file", None)
            fixed += 1

    by_id = _by_id(db.get("requests"))
    q = []
    for rid in (prefer_queue or []):
        rid = str(rid)
        r = by_id.get(rid)
        if r and r.get("state") == "queued" and rid not in q:
            q.append(rid)
    rest = sorted([r for r in (db.get("requests") or [])
                   if isinstance(r, dict) and r.get("state") == "queued"],
                  key=lambda x: float(x.get("created") or 0))
    for r in rest:
        rid = str(r.get("id"))
        if rid not in q:
            q.append(rid)
    db["queue"] = q

    # 容量裁剪：非终态（待批/排队）必须保住，其余按时间倒序补满
    def _num(x, k):
        try:
            return float(x.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    for key, cap, tkey in (("visits", _cap("MAX_VISITS", 400), "last"),
                           ("requests", _cap("MAX_REQ", 300), "created"),
                           ("wl_apps", _cap("WL_MAX_APPS", 300), "created"),
                           ("whitelist", _cap("WL_MAX_ENTRIES", 200), "last_seen")):
        arr = [x for x in (db.get(key) or []) if isinstance(x, dict)]
        if key == "requests" and len(arr) > cap:
            # 待批/排队的必须保住（那是访客还在等的），终态的按时间留最近的
            alive = [x for x in arr if x.get("state") in ("pending", "queued")]
            dead = sorted([x for x in arr if x.get("state") not in ("pending", "queued")],
                          key=lambda x: _num(x, tkey), reverse=True)
            if len(alive) > cap:
                # 连等着的都超上限（导进了几百条待批）：留最新的 cap 条，老的按过期处理，
                # 否则一次导入就能把队列灌到点不动
                alive = sorted(alive, key=lambda x: _num(x, tkey), reverse=True)[:cap]
                dead = []
            arr = alive + dead[:max(0, cap - len(alive))]
        elif len(arr) > cap:
            arr = sorted(arr, key=lambda x: _num(x, tkey), reverse=True)[:cap]
        db[key] = arr
    return db, {"running_to_queued": fixed, "queue": len(db["queue"])}


def _cap(name, default):
    g = _guest()
    try:
        return int(getattr(g, name, default))
    except Exception:
        return default


# ---------------- 导入 ----------------
def _write_atomic(path, data: bytes):
    tmp = path + ".import_tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _reload():
    """让内存缓存跟上刚落盘的库（不然界面上还是旧数据，重启才恢复）。"""
    g = _guest()
    if g and hasattr(g, "_load"):
        try:
            g._load()
        except Exception:
            pass
    s = _settings()
    if s and hasattr(s, "load"):
        try:
            s.load()
        except Exception:
            pass


def _current_snapshot() -> dict:
    snap = {}
    # 访客库的内存副本才是「现在」：soft 写有 2 秒节流，且写盘可能一直在失败。
    # 只读文件会在回滚时把这期间新进来的申请一起丢掉。
    mem = _cur_db()
    if isinstance(mem, dict):
        snap["__guest_mem__"] = mem
    for arc, p in (("data/guest_db.json", _db_path()),
                   ("data/advanced_settings.json", _settings_path()),
                   ("data/covers_index.json", _index_path())):
        if os.path.isfile(p):
            try:
                snap[arc] = _read_bytes(p)
            except OSError:
                pass
    return snap


def _restore(snapshot: dict):
    mem = snapshot.get("__guest_mem__")
    if isinstance(mem, dict) and _replace_guest(mem):
        # 内存里那份已经原样写回，文件也由 replace_db 落盘了，不用再按文件覆盖
        snapshot = {k: v for k, v in snapshot.items() if k != "data/guest_db.json"}
    for arc, data in snapshot.items():
        if arc == "__guest_mem__":
            continue
        if arc == "data/guest_db.json":
            obj = _loads(data)
            # 回滚也要走 guest 的写入口，否则内存里还是「导入后」的内容，
            # 下一次 _save() 会把回滚掉的版本又写回去
            if obj is not None and _replace_guest(obj):
                continue
        p = {"data/guest_db.json": _db_path(),
             "data/advanced_settings.json": _settings_path(),
             "data/covers_index.json": _index_path()}[arc]
        _write_atomic(p, data)
    _reload()


def apply(zip_path, token="", mode="replace") -> dict:
    """真正落盘。两种模式：

        replace  用备份整份覆盖本机数据（换机器 / 恢复出厂那一份）
        merge    按业务主键合并（两台机器各自攒了一批记录，合成一份）
    """
    mode = "merge" if str(mode or "").lower() == "merge" else "replace"
    with _LOCK:
        c = check(zip_path)
        if not c.get("ok"):
            return c
        if token and c.get("token") and str(token) != c["token"]:
            return {"ok": False, "msg": "这份备份在预检之后被改动过，为安全起见请重新选一次"}
        p = c["path"]

        # 导入前先整体存档：这是失败回滚的底，也是用户自己的后悔药。
        # 全新机器（本来就没数据）打包会返回 empty，那不是错误，继续导入即可。
        pre = export(kind="auto")
        if not pre.get("ok") and not pre.get("empty"):
            return {"ok": False, "msg": "导入前的自动备份没做成（%s），为安全起见已中止"
                                        % pre.get("msg")}
        pre_path = pre.get("path") if pre.get("ok") else None
        pre_name = pre.get("name") if pre.get("ok") else None

        before = _current_snapshot()
        added_audio_tmp = None
        try:
            with zipfile.ZipFile(p) as zf:
                names = set(zf.namelist())
                raw = {}
                for arc in ("data/guest_db.json", "data/advanced_settings.json",
                            "data/covers_index.json"):
                    if arc in names:
                        raw[arc] = zf.read(arc)

                stat = {}
                warnings = []
                # ---- 访客库 ----
                if "data/guest_db.json" in raw:
                    inc = _loads(raw["data/guest_db.json"])
                    if inc is None:
                        raise RuntimeError("备份里的访客库不是合法 JSON")
                    cur = _cur_db()
                    if mode == "merge":
                        merged, stat = _merge_guest_db(cur or {}, inc)
                    else:
                        merged = {"visits": list(inc.get("visits") or []),
                                  "requests": list(inc.get("requests") or []),
                                  "wl_apps": list(inc.get("wl_apps") or []),
                                  "whitelist": list(inc.get("whitelist") or []),
                                  "queue": [str(x) for x in (inc.get("queue") or [])]}
                        stat = {"visits_added": len(merged["visits"]),
                                "requests_added": len(merged["requests"]),
                                "wl_apps_added": len(merged["wl_apps"]),
                                "whitelist_added": len(merged["whitelist"])}
                    merged, norm = _normalize(merged, prefer_queue=(
                        merged.get("queue") if mode == "replace" else None))
                    stat.update(norm)
                    # 必须经 guest 写（内存 + 文件一起换），不能只覆盖文件
                    if not _replace_guest(merged):
                        _write_atomic(_db_path(), _dump(merged))

                # ---- 高级设置（走 settings.save 的校验，坏值夹回默认） ----
                if "data/advanced_settings.json" in raw:
                    vals = _loads(raw["data/advanced_settings.json"])
                    if vals:
                        s = _settings()
                        if s:
                            r = s.save({k: v for k, v in vals.items()
                                        if not str(k).startswith("_")})
                            if r.get("warns"):
                                warnings += r["warns"]
                            if not r.get("ok"):
                                warnings.append("设置没能写入：%s" % r.get("msg"))
                        else:
                            warnings.append("settings.py 没加载，这次跳过了高级设置")

                # ---- 成品索引 ----
                if "data/covers_index.json" in raw:
                    try:
                        inc_idx = json.loads(raw["data/covers_index.json"].decode(
                            "utf-8", "ignore"))
                    except Exception:
                        inc_idx = None
                    if isinstance(inc_idx, list):
                        if mode == "replace":
                            out_idx = [x for x in inc_idx if isinstance(x, dict)]
                        else:
                            cur_idx = []
                            try:
                                cur_idx = json.loads(_read_bytes(_index_path()).decode(
                                    "utf-8", "ignore"))
                            except Exception:
                                cur_idx = []
                            cur_idx = cur_idx if isinstance(cur_idx, list) else []
                            seen = {str(x.get("name") or "") for x in cur_idx
                                    if isinstance(x, dict)}
                            out_idx = [x for x in cur_idx if isinstance(x, dict)]
                            for x in inc_idx:
                                if isinstance(x, dict) and str(x.get("name") or "") not in seen:
                                    out_idx.append(x)
                        out_idx.sort(key=lambda x: float(x.get("created") or 0), reverse=True)
                        _write_atomic(_index_path(),
                                      json.dumps(out_idx, ensure_ascii=False,
                                                 indent=1).encode("utf-8"))

                # ---- 成品音频：先全解到临时目录，成功了再整体搬进去 ----
                # 只解「预检已经校验过 sha256/大小」的音频条目。
                # 以前直接遍历 namelist：包里多塞一个不在清单内的
                # covers/同名.mp3，预检说没问题、导入却会覆盖本机成品。
                audio = [n for n in (c.get("audio_arcs") or []) if n in names]
                if audio:
                    cdir = _covers_dir()
                    os.makedirs(cdir, exist_ok=True)
                    added_audio_tmp = os.path.join(cdir, "_import_tmp_%d" % int(time.time()))
                    os.makedirs(added_audio_tmp, exist_ok=True)
                    for n in audio:
                        base = os.path.basename(n)
                        if not base.lower().endswith(".mp3"):
                            continue
                        with open(os.path.join(added_audio_tmp, base), "wb") as f:
                            f.write(zf.read(n))
                    for base in os.listdir(added_audio_tmp):
                        os.replace(os.path.join(added_audio_tmp, base),
                                   os.path.join(cdir, base))
                    os.rmdir(added_audio_tmp)
                    added_audio_tmp = None
                    stat["audio_imported"] = len([n for n in audio
                                                  if n.lower().endswith(".mp3")])
        except Exception as e:
            if added_audio_tmp and os.path.isdir(added_audio_tmp):
                shutil.rmtree(added_audio_tmp, ignore_errors=True)
            try:
                _restore(before)
            except Exception as e2:
                return {"ok": False,
                        "msg": "导入失败（%s），自动还原也失败了（%s）。"
                               "请手动用这份备份恢复：%s" % (e, e2, pre_path)}
            return {"ok": False,
                    "msg": "导入失败：%s: %s。已自动还原到导入前的状态。"
                           % (type(e).__name__, e),
                    "rolled_back": True, "backup": pre_path}

        _reload()
        after = summary()
        return {
            "ok": True,
            "mode": mode,
            "msg": ("已合并导入：新增 %d 条申请、%d 位访客。"
                    % (stat.get("requests_added", 0), stat.get("visits_added", 0))
                    if mode == "merge" else
                    "已用这份备份覆盖本机数据。"),
            "stats": stat,
            "warns": warnings,
            "backup": pre_path,
            "backup_name": pre_name,
            "counts": after["counts"],
            "audio_files": c.get("audio_files") or 0,
            "missing_models": c.get("missing_models") or [],
            "missing_models_n": c.get("missing_models_n") or 0,
        }


# ---------------- 备份目录里的某一份（供下载/打开） ----------------
def backup_abs(name):
    base = os.path.basename(str(name or "").replace("\\", "/"))
    if not base.lower().endswith(".zip") or base != str(name or "").replace("\\", "/"):
        return None
    p = os.path.join(BACKUP_DIR, base)
    return p if os.path.isfile(p) else None


_PICK_SRC = """# -*- coding: utf-8 -*-
import json
try:
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.focus_force()
    path = filedialog.askopenfilename(
        parent=root,
        title="选择要导入的 RVC 数据备份（.zip）",
        filetypes=[("RVC 数据备份", "*.zip"), ("所有文件", "*.*")])
    root.destroy()
    print(json.dumps({"ok": True, "path": path or ""}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"ok": False, "msg": "%s: %s" % (type(e).__name__, e)},
                     ensure_ascii=False))
"""


def pick_zip():
    """弹 Windows 文件选择框选备份包。

    为什么要开子进程：tkinter 跑在 HTTP 线程里不稳（主循环一崩面板就跟着挂），
    子进程即便退出码异常也只影响这一次选文件。和 model_import.pick_files 同一套做法。
    """
    if os.name != "nt":
        return {"ok": False, "msg": "文件选择框只在 Windows 上可用"}
    import subprocess
    import sys
    import tempfile

    # 独立临时文件：固定名会被其它进程预置/被并发请求互相覆盖
    fd, tmp = tempfile.mkstemp(prefix="_rvc_backup_pick_", suffix=".py")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_PICK_SRC)
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        p = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=300, creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "打开选择框超过 5 分钟没动作，已放弃"}
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = (p.stdout or "").strip().splitlines()
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("ok") and not obj.get("path"):
                return {"ok": True, "cancelled": True}
            return obj
    return {"ok": False, "msg": (p.stderr or p.stdout or "选择框没有返回结果").strip()[:300]}


def open_dir() -> dict:
    """在资源管理器里打开备份目录（用户要自己拷贝/删文件时用）。"""
    d = ensure_dir()
    try:
        os.startfile(d)      # ShellExecute，不会闪黑窗
    except Exception as e:
        return {"ok": False, "msg": "打不开资源管理器：%s" % e, "dir": d}
    return {"ok": True, "msg": "已在资源管理器打开备份目录", "dir": d}
