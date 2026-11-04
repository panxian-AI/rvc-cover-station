# -*- coding: utf-8 -*-
"""翻唱网页（并入监控面板，路径 /cover，端口与面板相同）。

职责：
    搜索网易云歌曲 → 选声音模型 → 调后端 3333 的 /convert → 成品入库 → 缓存管理

为什么放在面板里而不是单独起一个服务：
    面板本身就用 apienv 跑，已具备 gradio_client、模型清单、端口探测；
    单独起进程会让「一键启动」多一个要管的东西，且多占一个端口。

对外接口（都由 panel.py 的 Handler 转发进来）：
    GET  /api/cover/search?kw=&limit=     搜歌
    GET  /api/cover/models                可用声音模型
    POST /api/cover/start                 开始翻唱，立即返回 job_id
    GET  /api/cover/job?id=               查询任务进度
    GET  /api/cover/covers                成品缓存列表
    POST /api/cover/delete                删除一个成品
    GET  /api/cover/cache                 系统缓存占用
    POST /api/cover/clear                 清理系统缓存
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid

# ---------------- 常量 ----------------
# 路径由 paths.py 按本文件位置推导，搬盘/改盘符都不用改代码
from paths import API_DIR, COVERS_DIR, RVC_ROOT  # noqa: E402
import settings as settings_mod                 # noqa: E402 高级设置（并发数 + 推理参数）

NCM_BASE = "http://127.0.0.1:3002"
API_BASE = "http://127.0.0.1:3333"
WEIGHTS_DIR = os.path.join(RVC_ROOT, "assets", "weights")
INDEX_PATH = os.path.join(COVERS_DIR, "index.json")
BACKEND_TEMP = os.path.join(API_DIR, "temp")
BACKEND_OUTPUT = os.path.join(API_DIR, "output")

# 声线的显示名（只影响界面文案，实际推理仍按文件名走）
MODEL_LABELS = {
    "feiyu.pth": "小肥鱼（肥鱼声库）",
}

# 本机回环不走系统代理，否则会被代理截走返回 502
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------- 任务队列 ----------------
# 用 RLock：提交时要在「查成品库 + 登记在跑任务」这一小段里保持原子性，
# 否则两个请求同时打进来会都查不到成品、都去跑一遍。
_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_MAX_JOBS = 40

# 去重台账：复用键 -> 正在跑的 job id（同一首同一声线只跑一次）
_inflight: dict[str, str] = {}

# ---------------- 并发闸 ----------------
# 站长在控制台「高级设置」里调「同时制作数量」，这里每次取槽位都现读设置，
# 所以在跑的单还没结束前把上限调大，也能立刻多放行几单。
_slot_lock = threading.Lock()
_slots_used = 0


def slots_used() -> int:
    with _slot_lock:
        return _slots_used


def _acquire_slot(jid: str) -> None:
    """占一个制作位；满了就原地等（线程阻塞在 sleep 上，不占锁）。"""
    global _slots_used
    while True:
        cap = max(1, settings_mod.concurrency())
        with _slot_lock:
            if _slots_used < cap:
                _slots_used += 1
                return
            busy = _slots_used
        _update(jid, stage="排队中（已有 %d 单在做，上限 %d，等一个空位）…" % (busy, cap))
        time.sleep(1.0)


def _release_slot() -> None:
    global _slots_used
    with _slot_lock:
        _slots_used = max(0, _slots_used - 1)


def _new_job(**kw) -> str:
    jid = uuid.uuid4().hex[:12]
    with _lock:
        if len(_jobs) > _MAX_JOBS:
            # 清掉最早的已完成任务，避免无限增长
            done = sorted((j for j in _jobs.items() if j[1]["state"] in ("done", "error")),
                          key=lambda x: x[1]["created"])
            for k, _v in done[: len(_jobs) - _MAX_JOBS]:
                _jobs.pop(k, None)
        _jobs[jid] = {"state": "queued", "stage": "排队中", "created": time.time(),
                      "started": None, "ended": None, "file": None, "error": None,
                      "title": kw.get("title") or kw.get("song_id"), "model": kw.get("model"),
                      "key_shift": kw.get("key_shift"), "acapella": kw.get("acapella")}
    return jid


def _update(jid: str, **kw):
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def job_status(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return None
        out = dict(j)
    if out["started"]:
        end = out["ended"] or time.time()
        out["elapsed"] = round(end - out["started"], 1)
    else:
        out["elapsed"] = 0
    return out


def jobs_snapshot(recent=5):
    """自助翻唱的实时任务快照（给总面板用）。

    自助翻唱是站长自己用的入口：任务只活在 _jobs 里，不写访客队列，
    所以以前只有 /cover 页面自己看得到进度，控制台「制作队列」永远只有访客单。
    这里把「正在做 / 排队 / 最近完成」一次给全，控制台照着渲染就行。
    """
    now = time.time()
    with _lock:
        items = [(jid, dict(j)) for jid, j in _jobs.items()]
    run, queued, done = [], [], []
    for jid, j in items:
        st = str(j.get("state") or "")
        started = j.get("started")
        row = {"id": jid, "title": j.get("title") or "(未命名)",
               "model": os.path.basename(str(j.get("model") or "")).replace(".pth", ""),
               "key_shift": j.get("key_shift") or 0,
               "acapella": bool(j.get("acapella")),
               "state": st, "stage": j.get("stage") or "",
               "file": j.get("file"), "error": j.get("error"),
               "created": float(j.get("created") or 0),
               "ended": float(j.get("ended") or 0),
               "elapsed": round((j.get("ended") or now) - started, 1) if started else 0}
        if st == "running":
            run.append(row)
        elif st == "queued":
            queued.append(row)
        else:
            done.append(row)
    run.sort(key=lambda r: r["created"])
    queued.sort(key=lambda r: r["created"])
    done.sort(key=lambda r: (r["ended"] or r["created"]), reverse=True)
    # 进度条用的平均耗时：拿最近跑完的真实时长取中位数（异常值不参与）
    durs = sorted([r["elapsed"] for r in done
                   if r["elapsed"] and 5 <= r["elapsed"] <= 900])[-12:]
    avg = durs[len(durs) // 2] if durs else 60.0
    for r in run:
        el = float(r.get("elapsed") or 0)
        r["pct"] = int(min(92, max(3, el / avg * 100)))
        left = max(0.0, avg - el)
        r["left_text"] = "快好了" if left <= 5 else _dur_text(left)
    try:
        cap = max(1, settings_mod.concurrency())
    except Exception:
        cap = 1
    return {"ok": True, "running": run, "queued": queued, "recent": done[:recent],
            "used": slots_used(), "cap": cap, "total": len(run) + len(queued),
            "avg_sec": round(avg, 1), "avg_text": _dur_text(avg)}


# ---------------- 工具 ----------------
def _dur_text(sec):
    """秒数说成人话：45 秒 / 2 分 5 秒 / 1 分（进度条旁边的「还剩多久」用它）。"""
    try:
        n = int(round(max(0, float(sec))))
    except Exception:
        return ""
    if n < 60:
        return "%d 秒" % n
    m, s = divmod(n, 60)
    return ("%d 分 %d 秒" % (m, s)) if s else ("%d 分" % m)


def _json_get(url: str, timeout=40):
    with _OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _safe_name(s: str) -> str:
    """去掉 Windows 文件名非法字符"""
    s = re.sub(r'[\\/:*?"<>|]+', "_", str(s or "")).strip(" .")
    return s or "未命名"


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
    return "%.1f GB" % n


def _dir_size(path: str, only_ext: str | None = None) -> tuple[int, int]:
    files = total = 0
    if os.path.isdir(path):
        for root, _d, names in os.walk(path):
            for n in names:
                if only_ext and not n.lower().endswith(only_ext):
                    continue
                try:
                    total += os.path.getsize(os.path.join(root, n))
                    files += 1
                except OSError:
                    pass
    return files, total


def _clear_dir(path: str) -> tuple[int, int]:
    files = freed = 0
    if not os.path.isdir(path):
        return 0, 0
    for root, dirs, names in os.walk(path, topdown=False):
        for n in names:
            p = os.path.join(root, n)
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                files += 1
                freed += sz
            except OSError:
                pass
        for dn in dirs:
            try:
                os.rmdir(os.path.join(root, dn))
            except OSError:
                pass
    return files, freed


def gradio_temp_dir() -> str:
    d = os.environ.get("GRADIO_TEMP_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp", "gradio")


def trusted_src(path: str) -> bool:
    """后端返回的文件路径可不可信。

    为什么必须查：3333 是**独立进程**、还监听 0.0.0.0，面板只是用字符串接收它返回的
    路径，然后 shutil.copy2 复制进成品库、再 os.remove 删掉。万一 3333 不是我们的后端
    （端口被别的程序占了、被换成别的东西），它返回 C:\\Windows\\System32\\xxx.mp3，
    面板就会把系统文件复制进成品库并把原件删掉。

    所以只认「我们自己让它生成」的几个临时目录，且必须是 .mp3。
    """
    if not path or not str(path).lower().endswith(".mp3"):
        return False
    roots = [
        BACKEND_TEMP,
        BACKEND_OUTPUT,
        gradio_temp_dir(),
        os.path.join(os.path.dirname(COVERS_DIR), "_tts_work"),
        tempfile.gettempdir(),
    ]
    try:
        target = os.path.realpath(os.path.abspath(str(path)))
        if not os.path.isfile(target):
            return False
    except OSError:
        return False
    for root in roots:
        try:
            if not root or not os.path.isdir(root):
                continue
            base = os.path.realpath(os.path.abspath(root))
            if os.path.commonpath([target, base]) == base:
                return True
        except (ValueError, OSError):
            continue
    return False


# ---------------- 搜索 / 模型 ----------------
def search(kw: str, limit: int = 12) -> dict:
    kw = (kw or "").strip()[:40]
    if not kw:
        return {"ok": False, "msg": "请输入歌名"}
    limit = max(1, min(30, int(limit or 12)))
    url = "%s/search?keywords=%s&limit=%d" % (NCM_BASE, urllib.parse.quote(kw), limit)
    try:
        raw = _json_get(url)
    except Exception as e:
        return {"ok": False, "msg": "网易云搜索服务当前不可用（127.0.0.1:3002 没响应），请先在控制台里启动它"}

    songs = (raw.get("result") or {}).get("songs") or []
    out = []
    for s in songs[:limit]:
        artists = s.get("ar") or s.get("artists") or []
        album = s.get("al") or s.get("album") or {}
        out.append({
            "id": str(s.get("id")),
            "name": s.get("name") or "(无标题)",
            "artist": "/".join(a.get("name", "") for a in artists) or "未知",
            "album": album.get("name") or "",
            "duration": round((s.get("dt") or s.get("duration") or 0) / 1000),
        })
    return {"ok": True, "songs": out}


def models() -> dict:
    """列出可用声线。label 只是给人看的名字，实际参数仍用文件名。"""
    items = []
    if os.path.isdir(WEIGHTS_DIR):
        for f in sorted(os.listdir(WEIGHTS_DIR)):
            if f.lower().endswith(".pth"):
                p = os.path.join(WEIGHTS_DIR, f)
                item = {"name": f, "label": MODEL_LABELS.get(f.lower(), os.path.splitext(f)[0])}
                try:
                    item["mb"] = round(os.path.getsize(p) / 1048576, 1)
                except OSError:
                    item["mb"] = 0
                items.append(item)
    return {"ok": True, "models": items, "dir": WEIGHTS_DIR}


# ---------------- 成品库 ----------------
def _load_index() -> list:
    # 翻唱和朗读都会改这份 index.json，读写都用同一把锁，
    # 否则「读-改-写」交叉时会互相覆盖，丢掉一条成品记录。
    with _lock:
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _save_index(items: list) -> None:
    with _lock:
        os.makedirs(COVERS_DIR, exist_ok=True)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, INDEX_PATH)


def covers() -> dict:
    os.makedirs(COVERS_DIR, exist_ok=True)
    index = _load_index()
    existing = {f for f in os.listdir(COVERS_DIR) if f.lower().endswith(".mp3")}
    alive, seen = [], set()
    for it in index:
        name = it.get("name")
        if not name or name not in existing or name in seen:
            continue
        p = os.path.join(COVERS_DIR, name)
        it = dict(it)
        try:
            it["size"] = os.path.getsize(p)
            it["mtime"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
        except OSError:
            continue
        seen.add(name)
        alive.append(it)

    # 目录里有但索引没记的（手动丢进去的），也列出来
    for name in sorted(existing - seen):
        p = os.path.join(COVERS_DIR, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        alive.append({"name": name, "title": os.path.splitext(name)[0], "artist": "",
                      "model": "", "key_shift": None, "acapella": False,
                      "created": st.st_mtime, "size": st.st_size,
                      "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                      "orphan": True})

    alive.sort(key=lambda x: x.get("created") or 0, reverse=True)
    for it in alive:
        it["size_text"] = human_size(it.get("size"))
    return {"ok": True, "covers": alive, "total_bytes": sum(i.get("size", 0) for i in alive),
            "total_text": human_size(sum(i.get("size", 0) for i in alive)), "dir": COVERS_DIR}


def delete_cover(name: str) -> dict:
    name = os.path.basename(str(name or ""))
    if not name.lower().endswith(".mp3"):
        return {"ok": False, "msg": "文件名不合法"}
    p = os.path.join(COVERS_DIR, name)
    if not os.path.isfile(p):
        return {"ok": False, "msg": "文件不存在（可能已被删除）"}
    try:
        sz = os.path.getsize(p)
        os.remove(p)
    except OSError as e:
        return {"ok": False, "msg": "删除失败：%s" % e}
    _save_index([i for i in _load_index() if i.get("name") != name])
    return {"ok": True, "msg": "已删除 %s，释放 %s" % (name, human_size(sz))}


def file_abs(name: str) -> str | None:
    """取得可播放文件的绝对路径（只允许 covers 目录内的 mp3）"""
    name = os.path.basename(str(name or ""))
    if not name.lower().endswith(".mp3"):
        return None
    p = os.path.join(COVERS_DIR, name)
    return p if os.path.isfile(p) else None


# ---------------- 成品复用（同参数不重复生成） ----------------
def reuse_key(song_id, model, key_shift, acapella) -> str:
    """参数指纹：歌 + 声线 + 升降调 + 是否清唱，四样全同才算「同一首」。"""
    return "%s|%s|%d|%d" % (str(song_id or "").strip(),
                            os.path.basename(str(model or "")).lower(),
                            int(key_shift or 0),
                            1 if acapella else 0)


def _alive_file(name: str) -> int:
    """成品还在硬盘上就返回字节数，否则 0（索引里有记录但文件被删过的情况）。"""
    p = os.path.join(COVERS_DIR, os.path.basename(str(name or "")))
    try:
        if os.path.isfile(p):
            sz = os.path.getsize(p)
            return sz if sz > 1024 else 0
    except OSError:
        pass
    return 0


def find_cached(song_id, model, key_shift, acapella) -> dict | None:
    """在成品库里找参数完全一致、文件还在的那个成品（新的优先）。

    只认 index.json 里登记过的；手动丢进目录的文件没有 song_id，无法判断是哪首歌。
    """
    song_id = str(song_id or "").strip()
    if not song_id:
        return None
    want = reuse_key(song_id, model, key_shift, acapella)
    model_low = os.path.basename(str(model or "")).lower()
    for it in reversed(_load_index()):
        if str(it.get("song_id") or "").strip() != song_id:
            continue
        if os.path.basename(str(it.get("model") or "")).lower() != model_low:
            continue
        try:
            key_shift_i = int(it.get("key_shift") or 0)
        except (TypeError, ValueError):
            key_shift_i = 0
        if reuse_key(song_id, it.get("model"), key_shift_i, bool(it.get("acapella"))) != want:
            continue
        sz = _alive_file(it.get("name"))
        if not sz:
            continue        # 记录还在、文件没了 → 当作没做过，重新生成
        return {"name": os.path.basename(it["name"]), "size": sz,
                "size_text": human_size(sz), "title": it.get("title", ""),
                "artist": it.get("artist", ""), "model": it.get("model", ""),
                "model_label": MODEL_LABELS.get(model_low, os.path.splitext(model_low)[0]),
                "key_shift": key_shift_i, "acapella": bool(it.get("acapella")),
                "created": it.get("created") or 0}
    return None


def cached_variants(song_id: str, limit: int = 8) -> list:
    """这首歌已经做过的成品清单（给访客页提示「已有成品，不用再等」）。"""
    song_id = str(song_id or "").strip()
    if not song_id.isdigit():
        return []
    out, seen = [], set()
    for it in reversed(_load_index()):
        if str(it.get("song_id") or "").strip() != song_id:
            continue
        try:
            key_shift_i = int(it.get("key_shift") or 0)
        except (TypeError, ValueError):
            key_shift_i = 0
        model = os.path.basename(str(it.get("model") or ""))
        fk = reuse_key(song_id, model, key_shift_i, bool(it.get("acapella")))
        if fk in seen or not _alive_file(it.get("name")):
            continue
        seen.add(fk)
        label = MODEL_LABELS.get(model.lower(), os.path.splitext(model)[0] or model)
        out.append({"model": model, "model_label": label,
                    "key_shift": key_shift_i, "acapella": bool(it.get("acapella")),
                    "text": "%s · %s%s" % (label,
                                           "原调" if not key_shift_i else
                                           ("升%d" % key_shift_i if key_shift_i > 0
                                            else "降%d" % abs(key_shift_i)),
                                           " · 清唱" if it.get("acapella") else ""),
                    "name": os.path.basename(it["name"])})
        if len(out) >= max(1, min(20, limit)):
            break
    return out


# ---------------- 系统缓存 ----------------
def cache_info() -> dict:
    areas = [
        ("covers", "我的翻唱（成品库）", COVERS_DIR),
        ("temp", "后端临时文件（含原始成品）", BACKEND_TEMP),
        ("output", "人声分离结果", BACKEND_OUTPUT),
        ("gradio", "Gradio 临时目录", gradio_temp_dir()),
    ]
    out, tf, tb = [], 0, 0
    for key, label, path in areas:
        # 成品库只统计 mp3，别把 index.json 也数进去
        f, b = _dir_size(path, ".mp3" if key == "covers" else None)
        out.append({"key": key, "label": label, "path": path,
                    "files": f, "bytes": b, "text": human_size(b)})
        tf += f
        tb += b
    return {"ok": True, "areas": out, "total_files": tf,
            "total_bytes": tb, "total_text": human_size(tb)}


def _clearable(path: str) -> bool:
    """清理目录前的安全闸：盘根、系统目录、用户主目录本身一律不动。

    这里被清的是「递归删除」，而路径有一处来自环境变量 GRADIO_TEMP_DIR ——
    万一是 C:\\ 或者 C:\\Users\\admin，一次点击就能把整块盘清空。所以先验形状。
    """
    try:
        p = os.path.realpath(os.path.abspath(str(path or "")))
    except (OSError, ValueError):
        return False
    if not os.path.isdir(p):
        return False
    drive, tail = os.path.splitdrive(p)
    parts = [x for x in re.split(r"[\\/]+", tail) if x]
    if len(parts) < 2:                       # C:\  或 C:\Temp 这种太顶层
        return False
    low = p.lower().rstrip("\\/")
    for b in (os.environ.get("WINDIR", r"C:\Windows"), r"C:\Program Files",
              r"C:\Program Files (x86)"):
        b = os.path.realpath(b).lower().rstrip("\\/")
        if low == b or low.startswith(b + os.sep):
            return False                     # 系统目录（含子目录）一律不清
    if low == os.path.realpath(os.path.expanduser("~")).lower().rstrip("\\/"):
        return False                         # 用户主目录本身不清（AppData 下的缓存照清）
    return True


def clear_cache(scope: str = "all") -> dict:
    scope = str(scope or "all")
    mapping = {
        "temp": [BACKEND_TEMP],
        "output": [BACKEND_OUTPUT],
        "gradio": [gradio_temp_dir()],
    }
    targets = []
    if scope == "all":
        for v in mapping.values():
            targets += v
    elif scope in mapping:
        targets = mapping[scope]
    else:
        return {"ok": False, "msg": "未知范围：%s（covers 不能在系统缓存里清，请到成品库逐条删）" % scope}

    skipped = [t for t in targets if not _clearable(t)]
    targets = [t for t in targets if _clearable(t)]
    if not targets:
        return {"ok": False, "msg": "这些目录看起来不安全（盘根 / 系统目录 / 用户主目录），已拒绝清理：%s"
                                    % "、".join(skipped)}

    dfiles = dfreed = 0
    for t in targets:
        f, b = _clear_dir(t)
        dfiles += f
        dfreed += b
    msg = "已清理 %d 个文件，释放 %s" % (dfiles, human_size(dfreed))
    if skipped:
        msg += "；跳过不安全路径：%s" % "、".join(skipped)
    return {"ok": True, "msg": msg, "deleted_files": dfiles, "freed_bytes": dfreed}


# ---------------- 翻唱任务 ----------------
def start_cover(payload: dict) -> dict:
    song_id = str(payload.get("song_id") or "").strip()
    # 只取文件名部分：模型名是用户可控输入，带 ../ 会指到 weights 目录外面
    model = os.path.basename(str(payload.get("model") or "").strip())
    title = str(payload.get("title") or song_id).strip()[:80]
    artist = str(payload.get("artist") or "").strip()[:60]
    try:
        key_shift = int(float(payload.get("key_shift") or 0))
    except (TypeError, ValueError):
        key_shift = 0
    acapella = bool(payload.get("acapella"))

    if not song_id.isdigit() or len(song_id) > 20:
        return {"ok": False, "msg": "歌曲 ID 不合法，请先搜索选歌"}
    if not model.lower().endswith(".pth"):
        return {"ok": False, "msg": "请选择一个声音模型"}
    if not os.path.isfile(os.path.join(WEIGHTS_DIR, model)):
        return {"ok": False, "msg": "模型文件不存在：%s" % model}
    if not (-24 <= key_shift <= 24):
        return {"ok": False, "msg": "升降调范围 -24 ~ 24"}

    key = reuse_key(song_id, model, key_shift, acapella)

    # ① 成品库里已有同参数的：直接把成品交出去，一个字节都不用重算
    hit = find_cached(song_id, model, key_shift, acapella)
    if hit:
        jid = _new_job(title=title, model=model, key_shift=key_shift, acapella=acapella,
                       song_id=song_id, artist=artist)
        now = time.time()
        _update(jid, state="done", started=now, ended=now,
                stage="复用已有成品（没有重复生成）", file=hit["name"],
                size=hit["size"], size_text=hit["size_text"], cached=True)
        return {"ok": True, "job": jid, "cached": True, "file": hit["name"],
                "msg": "这首歌这个声线之前做过，直接给你现成的（%s）" % hit["size_text"]}

    # ② 同参数正在做：挂到那个任务上，等同一个结果（避免两单抢 GPU 跑同一首）
    with _lock:
        run_jid = _inflight.get(key)
        run_job = _jobs.get(run_jid) if run_jid else None
        if not run_job or run_job.get("state") in ("done", "error"):
            _inflight.pop(key, None)
            run_jid = None
        else:
            return {"ok": True, "job": run_jid, "shared": True,
                    "msg": "这首同样的参数正在做，做好会一起给你"}
        jid = _new_job(title=title, model=model, key_shift=key_shift, acapella=acapella,
                       song_id=song_id, artist=artist)
        _inflight[key] = jid

    t = threading.Thread(target=_run_cover, args=(jid, song_id, title, artist, model,
                                                 key_shift, acapella, key), daemon=True)
    t.start()
    cap = max(1, settings_mod.concurrency())
    waiting = slots_used() >= cap
    return {"ok": True, "job": jid, "waiting": waiting,
            "msg": ("已排队：同时制作已达上限 %d 单，前面做完就轮到这首" % cap) if waiting
                   else "已开始，约 2~4 分钟"}


def _run_cover(jid, song_id, title, artist, model, key_shift, acapella, key=None):
    # 参数在「这一单真正开跑前」读一次：站长改设置对还没开始的任务立刻生效
    params = settings_mod.gen_params()
    _acquire_slot(jid)          # 并发上限：满了在这里等空位（状态仍是排队中）
    try:
        _update(jid, state="running", started=time.time(), stage="连接后端…")
        # gradio_client 较大，只在真正用时导入，避免拖慢面板启动
        from gradio_client import Client

        if not _port_open(3333):
            raise RuntimeError("后端 3333 没在运行，请先在面板里「一键全部启动」")

        _update(jid, stage="派发任务（下载 + 人声分离 + 推理）…")
        c = Client(API_BASE + "/", verbose=False)
        result = c.predict(
            song_name_src=song_id, key_shift=key_shift,
            vocal_vol=params["vocal_vol"], inst_vol=params["inst_vol"],
            model_dropdown=model,
            reverb_intensity=params["reverb_intensity"],
            delay_intensity=params["delay_intensity"],
            uvr5_agg=params["uvr5_agg"], uvr5_tta=False, uvr5_postprocess=False,
            uvr5_window_size=512, uvr5_high_end_process="mirroring",
            msst_batch_size=1, msst_num_overlap=4, msst_normalize=False,
            vocal_postprocess=False, shift_accompaniment=True,
            f0_method=params["f0_method"], index_rate=str(params["index_rate"]),
            filter_radius=params["filter_radius"],
            acapella=acapella,
            api_name="/convert",
        )
        if not result or not os.path.isfile(str(result)):
            raise RuntimeError("后端没有返回有效文件：%r" % (result,))

        _update(jid, stage="归档成品…")
        name = _archive(str(result), title, artist, model, key_shift, acapella, song_id)
        sz = os.path.getsize(os.path.join(COVERS_DIR, name))
        _update(jid, state="done", stage="完成", ended=time.time(), file=name,
                size=sz, size_text=human_size(sz))
    except Exception as e:
        _update(jid, state="error", stage="失败", ended=time.time(),
                error="%s: %s" % (type(e).__name__, e))
        traceback.print_exc()
    finally:
        _release_slot()         # 不管成败都还位，否则上限会被「漏算」卡死
        # 跑完（不管成败）就把台账摘掉，失败的单允许下次重跑
        if key:
            with _lock:
                if _inflight.get(key) == jid:
                    _inflight.pop(key, None)


def _port_open(port: int, host="127.0.0.1", timeout=0.8) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _archive(src: str, title: str, artist: str, model: str,
             key_shift: int, acapella: bool, song_id: str) -> str:
    """把成品收进 covers 目录并登记；顺手清掉源文件避免双份占盘"""
    if not trusted_src(src):
        raise RuntimeError("后端返回的文件不在允许的临时目录内，已拒绝处理：%s" % src)
    os.makedirs(COVERS_DIR, exist_ok=True)
    stem = _safe_name(title)[:70]
    if artist:
        stem += "（%s）" % _safe_name(artist)[:40]
    if key_shift:
        stem += "-%s%d" % ("升" if key_shift > 0 else "降", abs(key_shift))
    else:
        stem += "-原调"
    if acapella:
        stem += "-清唱"
    stem += "-" + _safe_name(os.path.splitext(model)[0])[:32]

    name = stem + ".mp3"
    n = 2
    while os.path.exists(os.path.join(COVERS_DIR, name)):
        name = "%s(%d).mp3" % (stem, n)
        n += 1

    dst = os.path.join(COVERS_DIR, name)
    shutil.copy2(src, dst)          # 先复制，成功后再删源，避免半截文件

    # 源文件可能是 gradio 缓存副本，也可能是后端 temp 里的原始成品，都清掉
    for cand in (src, os.path.join(BACKEND_TEMP, "%s-RVC-AI翻唱.mp3" % song_id)):
        try:
            if os.path.isfile(cand) and os.path.abspath(cand) != os.path.abspath(dst):
                os.remove(cand)
        except OSError:
            pass

    items = _load_index()
    items.append({"name": name, "title": title, "artist": artist, "model": model,
                  "key_shift": key_shift, "acapella": acapella, "song_id": song_id,
                  "created": time.time(), "size": os.path.getsize(dst)})
    _save_index(items)
    return name


# ---------------- 供 panel 调用的分发 ----------------
_PAGE_CACHE: str | None = None


def page() -> str:
    """翻唱页面的 HTML（与 panel.py 同目录的 cover_page.html）"""
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cover_page.html")
        with open(p, "r", encoding="utf-8") as f:
            _PAGE_CACHE = f.read()
    return _PAGE_CACHE


def handle_get(path: str, query: dict) -> tuple[int, dict]:
    q = query or {}
    try:
        if path == "/api/cover/search":
            return 200, search(q.get("kw", [""])[0] if isinstance(q.get("kw"), list) else q.get("kw", ""),
                               int((q.get("limit") or ["12"])[0] if isinstance(q.get("limit"), list) else (q.get("limit") or 12)))
        if path == "/api/cover/models":
            return 200, models()
        if path == "/api/cover/job":
            jid = (q.get("id") or [""])[0] if isinstance(q.get("id"), list) else q.get("id", "")
            st = job_status(jid or "")
            return (200, {"ok": True, "job": st}) if st else (404, {"ok": False, "msg": "任务不存在"})
        if path == "/api/cover/jobs":
            return 200, jobs_snapshot()
        if path == "/api/cover/covers":
            return 200, covers()
        if path == "/api/cover/exists":
            sid = (q.get("song_id") or [""])[0] if isinstance(q.get("song_id"), list) else q.get("song_id", "")
            variants = cached_variants(sid)
            model = os.path.basename(str(
                (q.get("model") or [""])[0] if isinstance(q.get("model"), list) else (q.get("model") or "")))
            acapella = str((q.get("acapella") or ["0"])[0] if isinstance(q.get("acapella"), list)
                           else (q.get("acapella") or "0")).lower() in ("1", "true", "yes")
            exact = bool(model) and any(
                v["model"].lower() == model.lower() and int(v["key_shift"] or 0) == 0
                and bool(v["acapella"]) == acapella for v in variants)
            return 200, {"ok": True, "variants": variants, "exact": exact,
                         "labels": [v["text"] for v in variants]}
        if path == "/api/cover/cache":
            return 200, cache_info()
    except Exception as e:
        traceback.print_exc()
        return 500, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    return 404, {"ok": False, "msg": "not found"}


def handle_post(path: str, body: dict) -> tuple[int, dict]:
    body = body or {}
    try:
        if path == "/api/cover/start":
            return 200, start_cover(body)
        if path == "/api/cover/delete":
            return 200, delete_cover(body.get("name"))
        if path == "/api/cover/clear":
            return 200, clear_cache(body.get("scope"))
    except Exception as e:
        traceback.print_exc()
        return 500, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    return 404, {"ok": False, "msg": "not found"}
