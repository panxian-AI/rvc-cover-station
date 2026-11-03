# -*- coding: utf-8 -*-
"""模型市场：搜免费 RVC 声线模型 → 多线路下载 → 核验 → 装进 WebUI。

为什么单独一个模块：
    模型不是「有目录就能用」——要先在公开模型站上搜到，再挑配套的 .index，
    下载几十上百 MB，还要确认下到的真是 RVC 声线（不是别家的 pth、不是半截文件），
    最后复制进 assets/weights + assets/indices。这一整套放在 panel.py 里会把路由淹掉。

数据来源（都在 SOURCES 里，可继续加）：
    models    HuggingFace 模型库（RVC 社区最主要的公开托管地，几百个声线）
    datasets  HuggingFace 数据集 —— 大量「模型合集包」，一个包里几十上百个 .pth
    两者都是官方公开 JSON API，等价于把网页上的列表/文件清单爬下来，不解析 HTML。

下载线路（LINES）：
    hf-mirror.com（国内镜像，默认最快）+ huggingface.co（官方）。
    每个文件下载前用 Range 真实探 256KB 测速，挑最快的线；失败自动换下一条。

核验（三道，缺一不可）：
    1) 大小必须和站上公布的一致；
    2) 有 SHA-256 就比对 SHA-256（HuggingFace 对 LFS 文件都给了）；
    3) 交给 model_import 用 torch 读权重结构，确认有 RVC 特征层（enc_p.*）。
    全过才装，装完再复核落盘大小。

安全设计：
    * 仓库名 / 文件路径都按不可信输入处理：正则挡 ../、反斜杠、绝对路径、控制字符；
    * 只允许 .pth / .index，大小上限 2GB / 1.5GB；
    * 目标目录固定（model_import 里的 weights / indices），临时文件只落在 _market_cache；
    * 只在本机控制台可调用（panel.py 的 /api/market/* 走 local_only + CSRF）。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.parse

import model_import
import speed
from paths import PROJ

CACHE_DIR = os.environ.get("RVC_MARKET_CACHE") or os.path.join(PROJ, "_market_cache")

# ---------------- 数据源 ----------------
SOURCES = {
    "models": {"label": "模型库", "api": "/api/models", "resolve": ""},
    "datasets": {"label": "合集包", "api": "/api/datasets", "resolve": "/datasets"},
}

DEFAULT_LINES = [
    {"id": "mirror", "label": "国内镜像", "base": "https://hf-mirror.com"},
    {"id": "official", "label": "官方线路", "base": "https://huggingface.co"},
]


# 自定义市场地址（模型站镜像 / 反代）落盘在这；RVC_MARKET_LINES 环境变量优先级更高
LINES_FILE = os.environ.get("RVC_MARKET_LINES_FILE") or os.path.join(PROJ, "_market_lines.json")
MAX_CUSTOM_LINES = 12
MAX_LINE_URL = 240


def normalize_base(url) -> str:
    """把用户填的地址洗成 https://host[/path]；不合规就抛 ValueError（中文原因）。"""
    raw = str(url or "").strip().strip('"').strip("'").replace(" ", "")
    if not raw:
        raise ValueError("地址不能为空")
    if len(raw) > MAX_LINE_URL:
        raise ValueError("地址太长（最多 %d 个字符）" % MAX_LINE_URL)
    if not re.match(r"^https?://", raw, re.I):
        raise ValueError("地址要以 http:// 或 https:// 开头")
    p = urllib.parse.urlsplit(raw)
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("只支持 http / https")
    if not p.netloc or "." not in p.netloc and ":" not in p.netloc:
        raise ValueError("地址里要带域名，例如 https://hf-mirror.com")
    if p.username or p.password:
        raise ValueError("地址里不要带用户名密码")
    if ".." in p.path or "\\" in p.path:
        raise ValueError("地址路径不合法")
    host = p.netloc.lower()
    path = p.path.rstrip("/")
    return "%s://%s%s" % (scheme, host, path)


def _line_id(base: str) -> str:
    return "c" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]


_cfg_lock = threading.Lock()
_cfg_cache = {"mtime": -1.0, "data": None}


def _empty_cfg() -> dict:
    return {"custom": [], "off": []}


def _load_cfg() -> dict:
    """读自定义线路配置。坏文件 / 缺文件都当空配置，不让市场整个挂掉。"""
    try:
        mt = os.path.getmtime(LINES_FILE)
    except OSError:
        return _empty_cfg()
    with _cfg_lock:
        if _cfg_cache["data"] is not None and mt == _cfg_cache["mtime"]:
            return _cfg_cache["data"]
    cfg = _empty_cfg()
    try:
        with open(LINES_FILE, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        seen = set()
        for item in (raw.get("custom") or [])[:MAX_CUSTOM_LINES * 2]:
            if not isinstance(item, dict):
                continue
            try:
                base = normalize_base(item.get("base"))
            except ValueError:
                continue
            lid = _line_id(base)
            if lid in seen:
                continue
            seen.add(lid)
            label = str(item.get("label") or "").strip()[:40] or base
            cfg["custom"].append({"id": lid, "label": label, "base": base})
        off = raw.get("off") or []
        if isinstance(off, list):
            cfg["off"] = [str(x) for x in off if isinstance(x, (str, int))][:40]
    with _cfg_lock:
        _cfg_cache["data"], _cfg_cache["mtime"] = cfg, mt
    return cfg


def _save_cfg(cfg: dict) -> None:
    tmp = LINES_FILE + ".tmp"
    payload = {"_note": "模型市场的自定义线路；控制台 /market 里可以增删",
               "custom": cfg["custom"], "off": cfg["off"]}
    try:
        os.makedirs(os.path.dirname(LINES_FILE) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, LINES_FILE)
    except OSError as e:
        raise RuntimeError("保存线路配置失败：%s" % e)
    with _cfg_lock:
        _cfg_cache["data"], _cfg_cache["mtime"] = cfg, os.path.getmtime(LINES_FILE)


def _env_lines() -> list:
    raw = (os.environ.get("RVC_MARKET_LINES") or "").strip()
    if not raw:
        return []
    out = []
    for i, part in enumerate(raw.split(",")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            label, base = part.split("=", 1)
        else:
            label, base = "线路%d" % (i + 1), part
        try:
            base = normalize_base(base)
        except ValueError:
            continue
        out.append({"id": "l%d" % i, "label": (label.strip() or base)[:40], "base": base,
                    "builtin": True, "enabled": True})
    return out


def all_lines() -> list:
    """全部线路（含被停用的），带 builtin / enabled 标记，给界面看。"""
    env = _env_lines()
    if env:
        return env
    cfg = _load_cfg()
    off = set(cfg["off"])
    out = [dict(x, builtin=True, enabled=x["id"] not in off) for x in DEFAULT_LINES]
    out += [{"id": c["id"], "label": c["label"], "base": c["base"],
             "builtin": False, "enabled": c["id"] not in off} for c in cfg["custom"]]
    return out


def _lines() -> list:
    """实际参与搜索/下载的线路：启用的那些。全停用则回落到内置线路。

    优先级：RVC_MARKET_LINES 环境变量 > _market_lines.json 里的自定义线路 > 内置两条。
    """
    env = _env_lines()
    if env:
        return [dict(x) for x in env]
    usable = [x for x in all_lines() if x.get("enabled")]
    if usable:
        return [{"id": x["id"], "label": x["label"], "base": x["base"]} for x in usable]
    return [dict(x) for x in DEFAULT_LINES]


LINES = _lines()

MAX_PTH = 2 * 1024 * 1024 * 1024        # 单个声线 2GB 上限
MAX_INDEX = 1536 * 1024 * 1024          # 单个索引 1.5GB 上限
MAX_FILES = 600                          # 详情里最多回多少个文件给前端
API_TIMEOUT = 25
CHUNK = 128 * 1024

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\u4e00-\u9fff]{0,80}/"
                      r"[A-Za-z0-9._\-\u4e00-\u9fff]{1,120}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")

# ---------------- 线路 / 接口 ----------------
_line_lock = threading.Lock()
_line_pref = {"line": "", "at": 0.0}


def _preferred_line() -> dict:
    """上一次成功的线路（5 分钟内优先它），省一次失败重试。"""
    lines = _lines()
    with _line_lock:
        lid, at = _line_pref["line"], _line_pref["at"]
    if lid and time.time() - at < 300:
        for ln in lines:
            if ln["id"] == lid:
                return ln
    return lines[0]


def _mark_line(lid: str) -> None:
    with _line_lock:
        _line_pref["line"], _line_pref["at"] = lid, time.time()


def api_url(line: dict, path: str) -> str:
    return line["base"] + path


def file_url(line: dict, kind: str, repo: str, path: str) -> str:
    """拼下载地址：仓库名保持原样，文件路径逐段 quote（空格、中文、# 都要转义）。"""
    src = SOURCES[kind]
    segs = [urllib.parse.quote(s, safe="") for s in path.split("/") if s]
    return "%s%s/%s/resolve/main/%s" % (line["base"], src["resolve"], repo,
                                        "/".join(segs))


def _get_json(path: str, timeout: float = API_TIMEOUT, prefer: dict | None = None) -> tuple:
    """按线路顺序取 JSON。返回 (data, line)。全失败抛最后一个异常。"""
    lines = _lines()
    if prefer:
        lines = [prefer] + [x for x in lines if x["id"] != prefer["id"]]
    else:
        first = _preferred_line()
        lines = [first] + [x for x in lines if x["id"] != first["id"]]
    last = None
    for ln in lines:
        try:
            raw = speed.fetch(api_url(ln, path), timeout=timeout,
                              headers={"Accept": "application/json"})
            data = json.loads(raw.decode("utf-8", "ignore"))
            _mark_line(ln["id"])
            return data, ln
        except Exception as e:
            last = e
            # 429 是「这个 IP 打太快了」，换线路只会更糟，直接告诉用户等一会儿
            if "429" in str(e):
                raise RuntimeError("模型站限流了（429），等半分钟再搜")
            continue
    raise last if last else RuntimeError("所有线路都不通")


def _explain(e: Exception) -> str:
    s = "%s: %s" % (type(e).__name__, e)
    low = s.lower()
    if "timed out" in low or "timeout" in low:
        return "连接模型站超时（线路拥堵或被墙），点「重试」或换线路再试"
    if "429" in low:
        return "模型站限流了（请求太频繁），等半分钟再搜"
    if "403" in low:
        return "模型站拒绝访问（可能查得太频繁，等一会儿再试）"
    if "404" in low:
        return "模型站上没有这个仓库（可能已被作者删除）"
    if "getaddrinfo" in low or "name or service" in low:
        return "域名解析失败（本机 DNS / 网络有问题）"
    if "certificate" in low:
        return "HTTPS 证书校验失败（网络被劫持或代理干扰）"
    return s[:160]


# ---------------- 搜索 ----------------
_SORT = {"downloads": ("downloads", -1), "likes": ("likes", -1),
         "recent": ("lastModified", -1)}


def _item(kind: str, m: dict) -> dict | None:
    repo = str(m.get("id") or "").strip()
    if not repo or "/" not in repo:
        return None
    if m.get("private") or m.get("disabled"):
        return None
    gated = m.get("gated")
    if gated not in (None, False, "false"):
        return None
    sib = m.get("siblings") or []
    names = [str(s.get("rfilename") or "") for s in sib if isinstance(s, dict)]
    pth = [x for x in names if x.lower().endswith(".pth")]
    if not pth:
        return None
    idx = [x for x in names if x.lower().endswith(".index")]

    # 光有 .pth 不够：Qwen / SDXL / LoRA 之类也存成 .pth。
    # 判据：有 .index（RVC 专属）> 打了语音转换标签 > 名字里带 rvc/voice。
    tags = {str(t).lower() for t in (m.get("tags") or [])}
    low = repo.lower()
    voice_tags = {"rvc", "voice-conversion", "audio-to-audio",
                  "singing-voice-conversion", "voice", "vits"}
    noisy_tags = {"text-generation", "image-text-to-text", "text-to-image",
                  "image-classification", "image-segmentation", "automatic-speech-recognition"}
    name_hint = ("rvc" in low) or ("voice" in low) or ("so-vits" in low)
    if not (idx or (tags & voice_tags) or name_hint):
        return None
    if (tags & noisy_tags) and not (idx or (tags & voice_tags)):
        return None

    ts = str(m.get("lastModified") or "")
    return {
        "kind": kind, "repo": repo,
        "name": repo.split("/")[-1][:80],
        "author": repo.split("/")[0][:60],
        "downloads": int(m.get("downloads") or 0),
        "likes": int(m.get("likes") or 0),
        "updated": ts[:10],
        "files": len(names),
        "pth_n": len(pth), "index_n": len(idx),
        "pth": pth[:6], "index": idx[:6],
        "gated": False,
    }


_search_cache = {}
_sc_lock = threading.Lock()
SEARCH_TTL = 300          # 同一个关键词 5 分钟内不重复打接口


def _cache_get(key):
    with _sc_lock:
        v = _search_cache.get(key)
    if v and time.time() - v[0] < SEARCH_TTL:
        return v[1], v[2]
    return None, ""


def _cache_put(key, items, line_label):
    with _sc_lock:
        if len(_search_cache) > 40:
            _search_cache.clear()
        _search_cache[key] = (time.time(), items, line_label)


def search(kind: str = "models", q: str = "", sort: str = "downloads",
           page: int = 1, per: int = 20) -> dict:
    """搜模型 / 合集。返回 {ok, items, total, page, more, source, line}。"""
    kind = kind if kind in SOURCES else "models"
    q = str(q or "").strip()[:80]
    per = max(6, min(int(per or 20), 40))
    page = max(1, min(int(page or 1), 6))
    sort_key, direction = _SORT.get(str(sort or "downloads"), _SORT["downloads"])

    key = (kind, q, sort_key)
    items, line_label = _cache_get(key)
    if items is None:
        # 一次抓 100 个本地切页：翻页不用等，也不会把接口刷爆
        if kind == "models":
            if q:
                path = "/api/models?search=%s&limit=100&full=true&sort=%s&direction=%d" % (
                    urllib.parse.quote(q), sort_key, direction)
            else:
                path = "/api/models?filter=rvc&limit=100&full=true&sort=%s&direction=%d" % (
                    sort_key, direction)
        else:
            path = "/api/datasets?search=%s&limit=40&full=true&sort=%s&direction=%d" % (
                urllib.parse.quote(q or "rvc"), sort_key, direction)
        try:
            raw, line = _get_json(path)
        except Exception as e:
            return {"ok": False, "msg": _explain(e), "items": [], "total": 0,
                    "page": page, "more": False, "kind": kind}
        if not isinstance(raw, list):
            return {"ok": False, "msg": "模型站返回了意外的数据格式", "items": [],
                    "total": 0, "page": page, "more": False, "kind": kind}
        if kind == "models":
            items = [it for it in (_item(kind, m) for m in raw if isinstance(m, dict)) if it]
            line_label = line["label"]
        else:
            # 合集包的 search 结果里 siblings 是空的，得看文件清单才知道有没有 .pth
            items = [it for it in (_ds_item(m) for m in raw if isinstance(m, dict)) if it]
            line_label = line["label"]
            from concurrent.futures import ThreadPoolExecutor
            # 只补前 8 个、3 路并发：HuggingFace 对匿名请求限流，一次打十几个会 429
            head = items[:8]
            try:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    head = list(ex.map(_fill_dataset, head))
            except Exception:
                head = [_fill_dataset(it) for it in head]
            items = [x for x in head if x and x.get("pth_n")]
        _cache_put(key, items, line_label)

    start = (page - 1) * per
    window = items[start:start + per]
    return {"ok": True, "kind": kind, "q": q, "page": page,
            "total": len(items), "more": len(items) > start + per,
            "items": window, "line": line_label}


def _ds_item(m: dict) -> dict | None:
    """合集包的搜索结果（还没有文件清单，等 _fill_dataset 补）。"""
    repo = str(m.get("id") or "").strip()
    if not repo or "/" not in repo or m.get("private") or m.get("disabled"):
        return None
    if m.get("gated") not in (None, False, "false"):
        return None
    return {"kind": "datasets", "repo": repo, "name": repo.split("/")[-1][:80],
            "author": repo.split("/")[0][:60],
            "downloads": int(m.get("downloads") or 0),
            "likes": int(m.get("likes") or 0),
            "updated": str(m.get("lastModified") or "")[:10],
            "files": 0, "pth_n": 0, "index_n": 0, "pth": [], "index": [], "gated": False}


def _fill_dataset(it: dict) -> dict | None:
    """给合集包补文件清单（只补前 8 个，避免一次打十几个接口被限流）。"""
    try:
        d, _ = _get_json("/api/datasets/%s" % it["repo"], timeout=15)
    except Exception:
        return it
    names = [str(s.get("rfilename") or "") for s in (d.get("siblings") or [])
             if isinstance(s, dict)]
    pth = [x for x in names if x.lower().endswith(".pth")]
    idx = [x for x in names if x.lower().endswith(".index")]
    it.update(files=len(names), pth_n=len(pth), index_n=len(idx),
              pth=pth[:6], index=idx[:6])
    return it


def detail(kind: str, repo: str) -> dict:
    """一个仓库的文件清单（带大小 / SHA-256），用于选文件与下载核验。"""
    kind = kind if kind in SOURCES else "models"
    repo = str(repo or "").strip()
    if not _REPO_RE.match(repo):
        return {"ok": False, "msg": "仓库名不合法"}
    path = "%s/%s?blobs=true" % (SOURCES[kind]["api"], repo)
    try:
        raw, line = _get_json(path)
    except Exception as e:
        return {"ok": False, "msg": _explain(e)}

    files, total = [], 0
    for s in (raw.get("siblings") or []):
        if not isinstance(s, dict):
            continue
        p = str(s.get("rfilename") or "")
        if not p or p.count("/") > 8 or len(p) > 300:
            continue
        size = s.get("size")
        lfs = s.get("lfs") or {}
        size = int(lfs.get("size") or size or 0)
        low = p.lower()
        ftype = "pth" if low.endswith(".pth") else ("index" if low.endswith(".index") else "other")
        item = {"path": p, "name": p.split("/")[-1], "size": size,
                "mb": round(size / 1048576.0, 1) if size else 0,
                "sha256": str(lfs.get("sha256") or ""),
                "type": ftype}
        files.append(item)
        if ftype != "other":
            total += size

    files.sort(key=lambda x: ({"pth": 0, "index": 1, "other": 2}[x["type"]], x["path"]))
    meta = {"downloads": int(raw.get("downloads") or 0),
            "likes": int(raw.get("likes") or 0),
            "updated": str(raw.get("lastModified") or "")[:10],
            "gated": raw.get("gated") not in (None, False, "false")}
    return {"ok": True, "kind": kind, "repo": repo, "line": line["label"],
            "files": files[:MAX_FILES], "truncated": len(files) > MAX_FILES,
            "pth_n": sum(1 for f in files if f["type"] == "pth"),
            "index_n": sum(1 for f in files if f["type"] == "index"),
            "total_mb": round(total / 1048576.0, 1), "meta": meta}


# ---------------- 线路状态 / 自定义地址 ----------------
_lines_cache = {"at": 0.0, "data": []}


def _clear_line_caches() -> None:
    with _line_lock:
        _lines_cache["at"], _lines_cache["data"] = 0.0, []
        _line_pref["line"], _line_pref["at"] = "", 0.0


def _probe(base: str, timeout: float = 8.0) -> dict:
    """单条地址实测：真发一个小 JSON 请求，返回延迟/错误原因。"""
    t0 = time.time()
    try:
        speed.fetch(base + "/api/models?search=rvc&limit=1", timeout=timeout,
                    headers={"Accept": "application/json"})
        return {"ok": True, "ms": int((time.time() - t0) * 1000), "error": ""}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": _explain(e)}


def line_test(base) -> dict:
    """「先测一下这个地址通不通」——不落盘。"""
    try:
        base = normalize_base(base)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    r = _probe(base)
    if not r["ok"]:
        return {"ok": False, "base": base, "ms": r["ms"],
                "msg": "这个地址连不上：%s" % r["error"]}
    return {"ok": True, "base": base, "ms": r["ms"], "msg": "通了，延迟 %d ms" % r["ms"]}


def line_add(label, base, who: str = "") -> dict:
    """加一条自定义市场地址（HF 兼容的镜像 / 反代）。"""
    if _env_lines():
        return {"ok": False, "msg": "当前用环境变量 RVC_MARKET_LINES 指定线路，"
                                    "界面上的改动不生效；想用界面管理请先去掉这个变量"}
    try:
        base = normalize_base(base)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in _load_cfg().items()}
    if any(x["base"].lower() == base.lower() for x in all_lines()):
        return {"ok": False, "msg": "这个地址已经在列表里了"}
    if len(cfg["custom"]) >= MAX_CUSTOM_LINES:
        return {"ok": False, "msg": "自定义地址最多 %d 条，先删掉几条" % MAX_CUSTOM_LINES}
    lid = _line_id(base)
    name = str(label or "").strip()[:40] or base.split("://", 1)[-1]
    cfg["custom"].append({"id": lid, "label": name, "base": base})
    if lid in cfg["off"]:
        cfg["off"] = [x for x in cfg["off"] if x != lid]
    _save_cfg(cfg)
    _clear_line_caches()
    r = _probe(base)
    msg = "已添加「%s」" % name
    if r["ok"]:
        msg += "，实测 %d ms" % r["ms"]
    else:
        msg += "，但现在连不上（%s）——先留着，等网络好了会自动参与测速" % r["error"]
    return {"ok": True, "id": lid, "base": base, "label": name,
            "reachable": r["ok"], "ms": r["ms"], "msg": msg}


def line_remove(lid) -> dict:
    lid = str(lid or "").strip()
    if _env_lines():
        return {"ok": False, "msg": "线路由环境变量 RVC_MARKET_LINES 指定，界面改不了"}
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in _load_cfg().items()}
    hit = [x for x in cfg["custom"] if x["id"] == lid]
    if not hit:
        return {"ok": False, "msg": "内置线路不能删，只能停用"}
    if not [x for x in all_lines() if x["id"] != lid and x["enabled"]]:
        return {"ok": False, "msg": "这是唯一启用的线路，删了就没地方下模型了；先启用别的线路"}
    cfg["custom"] = [x for x in cfg["custom"] if x["id"] != lid]
    cfg["off"] = [x for x in cfg["off"] if x != lid]
    _save_cfg(cfg)
    _clear_line_caches()
    return {"ok": True, "msg": "已删除「%s」" % hit[0]["label"]}


def line_toggle(lid, on=True) -> dict:
    lid = str(lid or "").strip()
    if _env_lines():
        return {"ok": False, "msg": "线路由环境变量 RVC_MARKET_LINES 指定，界面改不了"}
    target = [x for x in all_lines() if x["id"] == lid]
    if not target:
        return {"ok": False, "msg": "没有这条线路"}
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in _load_cfg().items()}
    off = [x for x in cfg["off"] if x != lid]
    if not on:
        if len([x for x in all_lines() if x["enabled"]]) <= 1:
            return {"ok": False, "msg": "至少得留一条线路，不然没地方下模型"}
        off.append(lid)
    cfg["off"] = off
    _save_cfg(cfg)
    _clear_line_caches()
    name = target[0]["label"]
    return {"ok": True, "msg": "已%s「%s」" % ("启用" if on else "停用", name)}


def lines_status(fresh: bool = False) -> dict:
    """给界面看的线路延迟（真发一个小请求，不是 ping）。"""
    env = bool(_env_lines())
    with _line_lock:
        if not fresh and _lines_cache["data"] and time.time() - _lines_cache["at"] < 120:
            return {"ok": True, "cached": True, "lines": _lines_cache["data"],
                    "custom_max": MAX_CUSTOM_LINES, "locked": env, "file": LINES_FILE}

    all_ln = all_lines()

    def one(ln):
        if not ln.get("enabled"):
            return dict(ln, ms=0, error="", skipped=True)
        r = _probe(ln["base"])
        return dict(ln, ok=r["ok"], ms=r["ms"], error=r["error"], skipped=False)

    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(all_ln))) as ex:
            data = list(ex.map(one, all_ln))
    except Exception:
        data = [one(ln) for ln in all_ln]
    with _line_lock:
        _lines_cache["at"], _lines_cache["data"] = time.time(), data
    return {"ok": True, "cached": False, "lines": data,
            "custom_max": MAX_CUSTOM_LINES, "locked": env, "file": LINES_FILE}


# ---------------- 下载任务 ----------------
_lock = threading.Lock()
_jobs = {}
_order = []
_seq = [0]
_workq = queue.Queue()
_worker = {"t": None}


class _Cancelled(Exception):
    pass


class _Fatal(Exception):
    """换线路也解决不了的错误（比如文件内容与公布的 SHA-256 不符）。"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean_repo_path(repo: str) -> str:
    return _SAFE_NAME.sub("_", repo.replace("/", "__"))


def _job_new(kind: str, repo: str, pth: str, index: str, replace: bool) -> dict:
    with _lock:
        _seq[0] += 1
        jid = "%d-%d" % (int(time.time()), _seq[0])
        job = {
            "id": jid, "kind": kind, "repo": repo, "pth": pth, "index": index,
            "replace": bool(replace), "state": "queued", "stage": "排队中",
            "msg": "", "done": 0, "total": 0, "speed": 0.0, "line": "",
            "file": "", "files_done": 0, "files_total": 1,
            "created": _now(), "started": "", "ended": "", "installed": {},
            "_cancel": False, "_base": 0, "_t0": 0.0, "_b0": 0, "_last": 0.0,
        }
        _jobs[jid] = job
        _order.append(jid)
        # 只留最近 40 条记录
        while len(_order) > 40:
            old = _order.pop(0)
            if _jobs.get(old, {}).get("state") in ("done", "error", "cancelled"):
                _jobs.pop(old, None)
            else:
                _order.append(old)
                break
    return job


def _job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def jobs() -> list:
    with _lock:
        return [_job_public(_jobs[i]) for i in reversed(_order) if i in _jobs]


def cancel(jid: str) -> dict:
    with _lock:
        job = _jobs.get(str(jid))
        if not job:
            return {"ok": False, "msg": "没有这个任务"}
        if job["state"] in ("done", "error", "cancelled"):
            return {"ok": False, "msg": "任务已经结束了"}
        job["_cancel"] = True
        job["stage"] = "正在取消…"
    return {"ok": True, "msg": "已请求取消（下载中的文件会留着，下次接着传）"}


def clear_finished() -> dict:
    with _lock:
        keep = []
        for jid in _order:
            j = _jobs.get(jid)
            if j and j["state"] in ("done", "error", "cancelled"):
                _jobs.pop(jid, None)
            elif j:
                keep.append(jid)
        _order[:] = keep
    return {"ok": True, "msg": "已清空完成的记录"}


def _validate(kind: str, repo: str, pth: str, index: str) -> tuple:
    if kind not in SOURCES:
        return None, "不认识的来源"
    if not _REPO_RE.match(repo or ""):
        return None, "仓库名不合法"
    pth = str(pth or "").strip()
    index = str(index or "").strip()
    for p, ext, cap in ((pth, ".pth", MAX_PTH), (index, ".index", MAX_INDEX)):
        if not p:
            continue
        if not p.lower().endswith(ext):
            return None, "只支持 %s 文件" % ext
        if len(p) > 300 or ".." in p or "\\" in p or p.startswith("/") or "\x00" in p:
            return None, "文件路径不合法"
    if not pth:
        return None, "必须选一个 .pth 声线文件"
    return {"pth": pth, "index": index}, ""


def start(kind: str, repo: str, pth: str, index: str = "", replace: bool = False) -> dict:
    """把「下载并安装」排进队列。"""
    ok, msg = _validate(kind, repo, pth, index)
    if not ok:
        return {"ok": False, "msg": msg}
    with _lock:
        for j in _jobs.values():
            if j["repo"] == repo and j["pth"] == ok["pth"] and \
                    j["state"] in ("queued", "running"):
                return {"ok": False, "msg": "这个模型已经在下载队列里了"}
    job = _job_new(kind, repo, ok["pth"], ok["index"], replace)
    _workq.put(job["id"])
    _ensure_worker()
    return {"ok": True, "id": job["id"], "msg": "已加入下载队列"}


def _ensure_worker() -> None:
    with _lock:
        t = _worker["t"]
        if t and t.is_alive():
            return
        th = threading.Thread(target=_worker_loop, name="market-dl", daemon=True)
        _worker["t"] = th
        th.start()


def _worker_loop() -> None:
    while True:
        try:
            jid = _workq.get(timeout=1.0)
        except queue.Empty:
            with _lock:
                if _workq.empty():
                    _worker["t"] = None
                    return
            continue
        with _lock:
            job = _jobs.get(jid)
        if not job:
            continue
        try:
            _run(job)
        except _Cancelled:
            with _lock:
                job.update(state="cancelled", stage="已取消", ended=_now())
        except Exception as e:
            with _lock:
                job.update(state="error", stage="失败", ended=_now(),
                           msg=str(e)[:300] or type(e).__name__)


def _set(job: dict, **kw) -> None:
    with _lock:
        job.update(kw)


def _run(job: dict) -> None:
    _set(job, state="running", stage="读取文件清单", started=_now())
    d = detail(job["kind"], job["repo"])
    if not d.get("ok"):
        raise RuntimeError(d.get("msg") or "读取文件清单失败")

    want = {f["path"]: f for f in d["files"]}
    pth = want.get(job["pth"])
    if not pth:
        raise RuntimeError("仓库里没有这个 .pth（可能被作者改名了，重新搜一次）")
    if pth["size"] and pth["size"] > MAX_PTH:
        raise RuntimeError("声线文件 %.1f GB，超过 2GB 上限" % (pth["size"] / 1073741824.0))
    index = want.get(job["index"]) if job["index"] else None
    if job["index"] and not index:
        raise RuntimeError("仓库里没有这个 .index（重新搜一次，或先只装声线）")
    if index and index["size"] and index["size"] > MAX_INDEX:
        raise RuntimeError("索引文件 %.1f GB，超过 1.5GB 上限" % (index["size"] / 1073741824.0))

    files = [pth] + ([index] if index else [])
    total = sum(int(f.get("size") or 0) for f in files)
    _set(job, total=total, files_total=len(files), done=0)

    got_paths = []
    base = 0
    for i, f in enumerate(files):
        if job["_cancel"]:
            raise _Cancelled()
        _set(job, _base=base, files_done=i,
             stage="下载 %d/%d · %s" % (i + 1, len(files), f["name"]), file=f["path"])
        dest = _download(job, job["kind"], job["repo"], f, i)
        _verify_file(job, f, dest)
        got_paths.append(dest)
        base += int(f.get("size") or 0)

    # 结构核验：确认这是 RVC 声线，不是别家的 pth
    _set(job, stage="核验模型结构（torch 读权重，约 10~30 秒）")
    info = model_import.inspect_pth(got_paths[0])
    if not info.get("ok"):
        raise RuntimeError("下载到的文件不是可用的 RVC 声线：%s" % info.get("msg"))

    _set(job, stage="装入 WebUI")
    res = model_import.install(got_paths[0],
                               got_paths[1] if len(got_paths) > 1 else None,
                               bool(job["replace"]),
                               name=pth["name"],
                               index_name=(index or {}).get("name") or None)
    if not res.get("ok"):
        raise RuntimeError(res.get("msg") or "安装失败")

    _set(job, state="done", stage="完成", progress=100, ended=_now(),
         installed={"name": res.get("pth"), "index": res.get("index"),
                    "sr": info.get("sr") or "", "version": info.get("version") or "",
                    "f0": info.get("f0") or "", "mb": info.get("mb") or 0},
         msg="已安装 %s%s。重启 3333 后端后新声线才会出现在点歌列表。"
             % (res.get("pth"), ("（索引 %s）" % res.get("index")) if res.get("index") else ""))


def sha256_file(path: str) -> str:
    """算文件 SHA-256（缓存复核、下载后校验共用）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            buf = fp.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _cache_path(kind: str, repo: str, path: str) -> str:
    safe = _SAFE_NAME.sub("_", repo.replace("/", "__")) + "__" + \
        _SAFE_NAME.sub("_", path.replace("/", "__"))
    if len(safe) > 150:
        safe = safe[:110] + "_" + hashlib.sha1(safe.encode("utf-8")).hexdigest()[:10]
    d = os.path.join(CACHE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, safe)


def _open_stream(url: str, start_at: int, timeout: float = 30):
    """打开下载流（支持 Range 续传）。直连优先，失败退系统代理。"""
    hdrs = {"User-Agent": speed.UA, "Accept-Encoding": "identity"}
    if start_at > 0:
        hdrs["Range"] = "bytes=%d-" % start_at
    attempts = [""]
    px = speed._env_proxy()
    if px:
        attempts.append(px)
    last = None
    for proxy in attempts:
        for insecure in (False, True):
            try:
                op, req = speed._open(url, timeout, hdrs, proxy, insecure)
                return op.open(req, timeout=timeout), insecure
            except Exception as e:
                last = e
                if "certificate" in str(e).lower():
                    continue
                break
    raise last if last else RuntimeError("下载连接失败")


def _download(job: dict, kind: str, repo: str, f: dict, idx: int) -> str:
    """下一个文件：先测速选线，再边下边算 SHA-256。返回本地路径。"""
    expect = f.get("size") or 0
    dest = _cache_path(kind, repo, f["path"])
    part = dest + ".part"

    # 已经下完并且大小对得上 → 复用前先复核 SHA-256（缓存也可能被改坏）
    if os.path.isfile(dest) and (not expect or os.path.getsize(dest) == expect):
        want = (f.get("sha256") or "").lower()
        if not want or sha256_file(dest) == want:
            _set(job, done=job.get("_base", 0) + os.path.getsize(dest))
            return dest
        try:
            os.remove(dest)
        except OSError:
            pass

    lines = _lines()
    cands = [{"label": ln["label"], "url": file_url(ln, kind, repo, f["path"]),
              "line": ln} for ln in lines]
    _set(job, stage=job["stage"] + " · 测速", speed=0.0)
    ranked = speed.rank(cands, nbytes=131072, timeout=7, workers=len(cands))
    if not ranked:
        raise RuntimeError("没有可用的下载线路")

    errs = []
    for c in ranked:
        if job["_cancel"]:
            raise _Cancelled()
        try:
            _set(job, line=c["label"], stage="下载 %d/%d · %s" % (
                idx + 1, job["files_total"], f["name"]), speed=0.0)
            return _stream_to(job, c["url"], dest, part, f)
        except _Cancelled:
            raise
        except _Fatal:
            raise                     # 内容不对，换线路也一样，别浪费时间
        except Exception as e:
            errs.append("%s: %s" % (c["label"], _explain(e)))
            continue
    raise RuntimeError("所有线路都没下下来 —— " + " / ".join(errs[:3]))


def _stream_to(job: dict, url: str, dest: str, part: str, f: dict) -> str:
    expect = f.get("size") or 0
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if expect and have >= expect:
        os.replace(part, dest)
        return dest

    h = hashlib.sha256()
    if have:
        with open(part, "rb") as fp:          # 续传：先把已有部分喂给哈希
            while True:
                buf = fp.read(1024 * 1024)
                if not buf:
                    break
                h.update(buf)

    resp, _insecure = _open_stream(url, have)
    try:
        code = getattr(resp, "status", 200)
        if have and code != 206:              # 服务器不支持续传 → 从头来
            have = 0
            h = hashlib.sha256()
        base = int(job.get("_base") or 0)
        mode = "ab" if have else "wb"
        t0, b0 = time.time(), have
        with open(part, mode) as out:
            while True:
                if job["_cancel"]:
                    raise _Cancelled()
                buf = resp.read(CHUNK)
                if not buf:
                    break
                out.write(buf)
                h.update(buf)
                have += len(buf)
                dt = time.time() - t0
                if dt >= 0.6:
                    kb = (have - b0) / 1024.0
                    _set(job, done=base + have, speed=round(kb / dt, 1))
                    if not job.get("total"):
                        _set(job, total=base + have)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if expect and have != expect:
        raise RuntimeError("下载不完整（%d / %d 字节），可能线路中断了" % (have, expect))
    want = (f.get("sha256") or "").lower()
    if want and h.hexdigest() != want:
        try:
            os.remove(part)
        except OSError:
            pass
        raise _Fatal("SHA-256 校验不通过（文件被改过或下坏了），已丢弃")
    os.replace(part, dest)
    _set(job, done=base + have, speed=0.0)
    return dest


def _verify_file(job: dict, f: dict, path: str) -> None:
    size = os.path.getsize(path)
    if f.get("size") and size != f["size"]:
        raise RuntimeError("%s 大小不符（%d / %d）" % (f["name"], size, f["size"]))
    if f["type"] == "index":
        with open(path, "rb") as fp:
            head = fp.read(2)
        if head not in (b"Iw", b"Ix"):
            raise RuntimeError("%s 不是 faiss 索引（头部 %r）" % (f["name"], head))
    if f["type"] == "pth":
        with open(path, "rb") as fp:
            head = fp.read(4)
        if head[:2] != b"PK":
            raise RuntimeError("%s 不是 PyTorch 权重（头部 %r）" % (f["name"], head))


# ---------------- 本机已装模型 ----------------
def installed() -> dict:
    out = []
    try:
        names = os.listdir(model_import.WEIGHTS_DIR)
    except OSError:
        names = []
    idx_names = []
    try:
        idx_names = os.listdir(model_import.INDEX_DIR)
    except OSError:
        pass
    idx_low = [x.lower() for x in idx_names if x.lower().endswith(".index")]
    for n in sorted(names):
        if not n.lower().endswith(".pth"):
            continue
        p = os.path.join(model_import.WEIGHTS_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        base = model_import.index_base(n) or ""
        has_idx = any(base and base in model_import.index_base(x) for x in idx_low)
        out.append({"name": n, "mb": round(st.st_size / 1048576.0, 1),
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    "has_index": bool(has_idx)})
    return {"ok": True, "items": out, "n": len(out),
            "weights": model_import.WEIGHTS_DIR, "indices": model_import.INDEX_DIR}


def cache_info() -> dict:
    total, files = 0, 0
    for root, _dirs, fs in os.walk(CACHE_DIR):
        for fn in fs:
            try:
                total += os.path.getsize(os.path.join(root, fn))
                files += 1
            except OSError:
                pass
    return {"ok": True, "mb": round(total / 1048576.0, 1), "files": files,
            "path": CACHE_DIR}


def clear_cache() -> dict:
    with _lock:
        busy = any(j["state"] in ("queued", "running") for j in _jobs.values())
    if busy:
        return {"ok": False, "msg": "还有任务在跑，等它下完再清缓存"}
    import shutil
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    return {"ok": True, "msg": "缓存已清空"}


# ---------------- HTTP 入口 ----------------
def handle_get(path: str, q: dict) -> tuple:
    one = lambda k, d="": (q.get(k) or [d])[0]
    try:
        if path == "/api/market/search":
            return 200, search(one("kind", "models"), one("q"), one("sort", "downloads"),
                               int(one("page", "1") or 1), int(one("per", "20") or 20))
        if path == "/api/market/detail":
            return 200, detail(one("kind", "models"), one("repo"))
        if path == "/api/market/jobs":
            return 200, {"ok": True, "items": jobs()}
        if path == "/api/market/installed":
            return 200, installed()
        if path == "/api/market/lines":
            return 200, lines_status(one("fresh") in ("1", "true"))
        if path == "/api/market/line_test":
            return 200, line_test(one("base"))
        if path == "/api/market/cache":
            return 200, cache_info()
    except Exception as e:
        return 200, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    return 404, {"ok": False, "msg": "not found"}


def handle_post(path: str, body: dict) -> tuple:
    body = body if isinstance(body, dict) else {}
    try:
        if path == "/api/market/install":
            return 200, start(str(body.get("kind") or "models"),
                              str(body.get("repo") or ""),
                              str(body.get("pth") or ""),
                              str(body.get("index") or ""),
                              bool(body.get("replace")))
        if path == "/api/market/cancel":
            return 200, cancel(str(body.get("id") or ""))
        if path == "/api/market/clear":
            return 200, clear_finished()
        if path == "/api/market/cache_clear":
            return 200, clear_cache()
        if path == "/api/market/line_add":
            return 200, line_add(body.get("label"), body.get("base"))
        if path == "/api/market/line_remove":
            return 200, line_remove(body.get("id"))
        if path == "/api/market/line_toggle":
            return 200, line_toggle(body.get("id"), bool(body.get("on", True)))
    except Exception as e:
        return 200, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    return 404, {"ok": False, "msg": "not found"}
