# -*- coding: utf-8 -*-
"""数据备份与迁移 · 导出 / 备份目录。

只负责「把本机数据打包成 zip」与备份文件本身的增删查，不碰导入。
导入在 restore.py —— 那边会反过来调用这里的 export() 做导入前存档。
"""
from __future__ import annotations

import json
import os
import socket
import time
import zipfile

from .core import (APP, AUDIO_PREFIX, BACKUP_DIR, DATA, HERE, KIND, MAX_KEEP_AUTO,
                   MAX_KEEP_MANUAL, MAX_ZIP, PROJ, SAFE_ARCS, SCHEMA, _LOCK,
                   _LAST_EXPORT, _cover_counts, _covers_dir, _cur_db, _db_path,
                   _dump, _guest, _human, _index_path, _loads, _models_manifest,
                   _new_path, _read_bytes, _sha, _settings_path, _tag, ensure_dir,
                   version, _counts)


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


def backup_abs(name):
    base = os.path.basename(str(name or "").replace("\\", "/"))
    if not base.lower().endswith(".zip") or base != str(name or "").replace("\\", "/"):
        return None
    p = os.path.join(BACKUP_DIR, base)
    return p if os.path.isfile(p) else None


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


def open_dir() -> dict:
    """在资源管理器里打开备份目录（用户要自己拷贝/删文件时用）。"""
    d = ensure_dir()
    try:
        os.startfile(d)      # ShellExecute，不会闪黑窗
    except Exception as e:
        return {"ok": False, "msg": "打不开资源管理器：%s" % e, "dir": d}
    return {"ok": True, "msg": "已在资源管理器打开备份目录", "dir": d}


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
