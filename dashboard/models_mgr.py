# -*- coding: utf-8 -*-
"""模型管理：盘点已装声线，支持删除（含配套索引）。

只看三个地方，不依赖 WebUI 在不在跑：
    weights = RVC-WebUI\\assets\\weights\\*.pth      声线本体
    indices = RVC-WebUI\\logs\\*.index
            + RVC-WebUI\\assets\\indices\\*.index    相似度索引

index 和模型怎么对上号：WebUI 训练出的索引名是
    added_IVF966_Flat_nprobe_1_mygf-tf_v2.index
前后两段（added_IVF..._nprobe_1_ / _v2）只是训练参数，中间的 mygf-tf 才对应
模型 mygf-tf.pth —— 归一化后相等算配套；模型名出现在索引名里也算（弱匹配）。

删除接口是两段式的：第一次不传 confirm 只返回「会删哪些文件、腾多少空间」，
前端弹确认框；真正删除必须 confirm=true。文件名一律取 basename 且必须在
weights / indices 目录里，杜绝 .. 路径穿越。
"""
import os
import re
import time

from paths import RVC_ROOT

WEIGHTS_DIR = os.path.join(RVC_ROOT, "assets", "weights")
INDEX_DIRS = (os.path.join(RVC_ROOT, "logs"), os.path.join(RVC_ROOT, "assets", "indices"))


def _index_key(name):
    """把 index 文件名归一成「模型名」（与 RVCSVC-API/app_rvc.py 的同名逻辑一致）。"""
    n = re.sub(r"^added_IVF\d+_Flat_nprobe_\d+_", "", name, flags=re.I)
    n = re.sub(r"(_v\d+)?(_index)?$", "", n, flags=re.I)
    return n or name


def _collect_indices():
    out = []
    for d in INDEX_DIRS:
        try:
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(".index"):
                    out.append((fn, os.path.join(d, fn)))
        except OSError:
            pass
    return out


def _safe_join(base, name):
    """只允许纯文件名落在本目录里；返回绝对路径或 None。"""
    if not name or os.path.basename(name) != name or name in (".", ".."):
        return None
    if os.path.isabs(name) or "\\" in name or "/" in name:
        return None
    p = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(p).lower() != os.path.realpath(base).lower():
        return None
    return p


def _match_indices(model_name):
    """返回与该模型配套的 index 文件名列表。

    只认「名字相等」或「以 模型名 + 分隔符 开头」，绝不做裸子串包含：
    RVC 的索引名形如 added_IVF966_Flat_nprobe_1_<模型名>_v2.index，
    以前 `base in stem` 会让模型 `1.pth` / `ai.pth` 命中一大堆无关索引，
    删声线时把别人的 faiss 索引一起删掉（索引只能重训，等于毁数据）。
    """
    base = os.path.splitext(model_name)[0].lower().strip()
    if not base:
        return []
    hits = []
    for fn, _p in _collect_indices():
        stem = os.path.splitext(fn)[0].lower()
        key = _index_key(stem)
        if key == base or stem == base:
            hits.append(fn)
            continue
        # 模型名太短时不做任何前缀匹配（"ai"/"1" 这类会误伤一大片）
        if len(base) < 4:
            continue
        if any(stem.startswith(base + sep) or stem.endswith(sep + base)
               for sep in ("_", "-", ".", " ")):
            hits.append(fn)
    return hits


def list_models():
    models, total = [], 0
    try:
        names = sorted(os.listdir(WEIGHTS_DIR))
    except OSError:
        names = []
    for fn in names:
        p = os.path.join(WEIGHTS_DIR, fn)
        if not fn.lower().endswith(".pth") or not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        total += st.st_size
        models.append({
            "name": fn,
            "mb": round(st.st_size / 1048576, 1),
            "mtime": int(st.st_mtime),
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "indices": _match_indices(fn),
        })
    # 没有配套模型的「孤儿」索引，单列出来（占地方但没用了）
    used = {i for m in models for i in m["indices"]}
    orphans = []
    for fn, p in _collect_indices():
        if fn in used:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        orphans.append({
            "name": fn, "mb": round(st.st_size / 1048576, 1),
            "mtime": int(st.st_mtime),
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        })
    return {"ok": True, "dir": WEIGHTS_DIR, "models": models, "orphans": orphans,
            "count": len(models), "total_mb": round(total / 1048576, 1)}


def _plan_delete(name):
    """算「删它会动哪些文件」，不真删。返回 (错误信息或 None, 文件列表)。"""
    if name.lower().endswith(".pth"):
        p = _safe_join(WEIGHTS_DIR, name)
        if not p:
            return "模型名不合法（只能删 weights 目录里的 .pth 文件）", []
        if not os.path.isfile(p):
            return "weights 目录里没有这个模型（可能已删过）", []
        files = [(p, os.path.getsize(p))]
        for fn in _match_indices(name):
            for d in INDEX_DIRS:
                ip = _safe_join(d, fn)
                if ip and os.path.isfile(ip):
                    files.append((ip, os.path.getsize(ip)))
        return None, files
    if name.lower().endswith(".index"):
        for d in INDEX_DIRS:
            p = _safe_join(d, name)
            if p and os.path.isfile(p):
                return None, [(p, os.path.getsize(p))]
        return "indices 目录里没有这个索引（可能已删过）", []
    return "只能删除 .pth 声线或 .index 索引", []


def handle_post(path, body):
    if path != "/api/models/delete":
        return 404, {"ok": False, "msg": "未知接口"}
    name = str(body.get("name") or "").strip()
    if not name:
        return 400, {"ok": False, "msg": "缺少 name"}
    err, files = _plan_delete(name)
    if err:
        return 400, {"ok": False, "msg": err}
    if not body.get("confirm"):
        return 200, {"ok": True, "preview": True, "count": len(files),
                     "files": [os.path.basename(p) for p, _s in files],
                     "mb": round(sum(s for _p, s in files) / 1048576, 1),
                     "msg": "预览成功，带上 confirm=true 再真正删除"}
    freed = 0
    deleted = []
    for p, _s in files:
        try:
            os.remove(p)
            freed += _s
            deleted.append(os.path.basename(p))
        except OSError as e:
            return 500, {"ok": False, "msg": "删除 %s 失败: %s" % (os.path.basename(p), e),
                         "deleted": deleted}
    return 200, {"ok": True, "deleted": deleted,
                 "mb": round(freed / 1048576, 1),
                 "msg": "已删除 %d 个文件，腾出 %.1f MB" % (len(deleted), freed / 1048576)}


_PAGE_CACHE = {}


def page():
    """读 models_page.html（mtime 缓存，改完刷新浏览器即生效）。"""
    here = os.path.dirname(os.path.realpath(__file__))
    p = os.path.join(here, "models_page.html")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return "<h1>模型管理页面缺失</h1><p>dashboard/models_page.html 不见了</p>"
    hit = _PAGE_CACHE.get("models_page")
    if hit and hit[0] == mt:
        return hit[1]
    with open(p, "r", encoding="utf-8") as f:
        txt = f.read()
    _PAGE_CACHE["models_page"] = (mt, txt)
    return txt
