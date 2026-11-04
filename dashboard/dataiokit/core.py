# -*- coding: utf-8 -*-
"""数据备份与迁移 · 基础层。

常量、路径适配、锁、以及一堆没有业务含义的小工具。**不 import 上层**
（backup / restore），保持单向依赖：core <- backup <- restore。

路径为什么都要走函数（`_db_path()` 而不是常量）：
    guest / settings / cover 三个模块可能在面板启动早期还没加载，或者被测试
    换成了临时目录。写成常量会把「导入那一刻的路径」焊死，换机器/测试就错位。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from paths import COVERS_DIR, DATA, PROJ, RVC_ROOT

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _cap(name, default):
    g = _guest()
    try:
        return int(getattr(g, name, default))
    except Exception:
        return default
