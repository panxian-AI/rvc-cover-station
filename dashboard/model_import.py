# -*- coding: utf-8 -*-
"""模型导入：选文件 → 识别校验 → 装进 WebUI。

三个动作，全部只在本机控制台可触发（panel.py 的 /api/admin/import_* 路由，
local_only + CSRF 双闸）：

  pick_files()   弹出 Windows 文件选择框（tkinter，多选 .pth/.index），拿全路径。
                 在独立子进程里弹窗——tkinter 跑在 HTTP 线程里不稳定，
                 子进程崩了也不影响面板本身。
  check(paths)   逐个识别：
                   .pth  → torch 读权重头，判断「是不是 RVC 声线模型」，
                           归一化出 采样率/版本/f0/epoch/作者 等元信息；
                   .index → 验 faiss 魔数（Iw/Ix 开头），提取索引基名；
                 并尝试把 index 与 pth 自动配对（基名包含 / 相似度）。
  install(...)   把 pth 复制进 assets\\weights、index 复制进 assets\\indices，
                 目标名做白名单净化；同名默认拒绝，需显式 replace 覆盖。

安全设计：这个接口虽然只认本机，仍按「路径不可信」处理——
  * 只接受 isfile 的 .pth/.index；basename 净化后才拼目标路径（防 ../）；
  * 复制目标固定在 WebUI 的 weights/indices 目录里，不拼其他位置；
  * 大小上/下限（1MB ~ 4GB）先挡一波，再交给 torch 识别；
  * torch.load 用 weights_only=True，不执行任何 pickle 代码体。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from paths import RVC_ROOT

WEIGHTS_DIR = os.path.join(RVC_ROOT, "assets", "weights")
INDEX_DIR = os.path.join(RVC_ROOT, "assets", "indices")

MAX_FILES = 12            # 一次最多选 12 个文件
MIN_PTH_BYTES = 1024 * 1024        # 小于 1MB 不可能是声线模型
MAX_PTH_BYTES = 4 * 1024 * 1024 * 1024   # 4GB 上限，防拖一个巨型文件进来
MIN_INDEX_BYTES = 1024             # 索引至少 1KB

VALID_SR = ("32k", "40k", "48k")

# 文件选择框脚本：跑在子进程里，输出一行 JSON
_PICK_SRC = """# -*- coding: utf-8 -*-
import json
try:
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.focus_force()
    paths = filedialog.askopenfilenames(
        parent=root,
        title="选择 RVC 声线模型（.pth 与/或 .index 索引，可按住 Ctrl 多选）",
        filetypes=[("RVC 模型文件", "*.pth *.index"), ("所有文件", "*.*")])
    root.destroy()
    print(json.dumps({"ok": True, "paths": list(paths)}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"ok": False, "msg": "%s: %s" % (type(e).__name__, e)},
                     ensure_ascii=False))
"""


def pick_files():
    """弹 Windows 文件选择框，返回 {"ok", "paths", "cancelled"/"msg"}。"""
    if os.name != "nt":
        return {"ok": False, "msg": "文件选择框只在 Windows 上可用"}
    # 每次用独立临时文件：固定名字时两个并发请求会互相覆盖/删除对方的脚本，
    # 而且 %TEMP% 里任何进程都能预置同名文件（面板会以本机权限执行它）。
    fd, tmp = tempfile.mkstemp(prefix="_rvc_model_pick_", suffix=".py")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_PICK_SRC)
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        # 10 分钟没动静按放弃算（HTTP 线程一直挂着会占并发名额）
        p = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=600, creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "打开选择框超过 10 分钟没动作，已放弃"}
    except Exception as e:
        return {"ok": False, "msg": "无法打开文件选择框: %s" % e}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    line = ""
    for ln in (p.stdout or "").splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            line = ln
    if not line:
        return {"ok": False, "msg": "文件选择框没有返回结果"}
    try:
        r = json.loads(line)
    except ValueError:
        return {"ok": False, "msg": "文件选择框返回了无法解析的结果"}
    r.setdefault("paths", [])
    if r.get("ok") and not r["paths"]:
        r["cancelled"] = True
        r["msg"] = "没有选择文件"
    return r


def _clean_name(name):
    """文件名净化：只留盘符路径里合法的字符，其余换成下划线。"""
    name = os.path.basename(str(name or "").strip())
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" ._")
    return name


def _norm_sr(v):
    """采样率归一化：40000→40k，"40K"→40k。认不出返回 None。"""
    if isinstance(v, (int, float)) and v:
        v = int(v)
        for k in ("32", "40", "48"):
            if v == int(k) * 1000:
                return k + "k"
        return None
    s = str(v or "").strip().lower()
    m = re.match(r"^(\d{2})k?$", s)
    if m and m.group(1) + "k" in VALID_SR:
        return m.group(1) + "k"
    return None


def _norm_f0(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return None


def inspect_pth(path):
    """读一个 .pth，判断是不是 RVC 声线模型。永不抛异常，结果都放返回值里。"""
    info = {"path": path, "name": os.path.basename(path), "type": "pth"}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        info.update(ok=False, msg="读不到文件: %s" % e)
        return info
    info["mb"] = round(size / 1048576, 1)
    if size < MIN_PTH_BYTES:
        info.update(ok=False, msg="文件太小（%d KB），不可能是声线模型" % (size // 1024))
        return info
    if size > MAX_PTH_BYTES:
        info.update(ok=False, msg="文件超过 4GB，超出支持的模型大小")
        return info
    try:
        import torch
    except Exception as e:
        info.update(ok=False, msg="本机环境缺 torch，无法识别: %s" % e)
        return info
    try:
        d = torch.load(path, map_location="cpu", weights_only=True)
    except Exception as e:
        info.update(ok=False,
                    msg="不是有效的 torch 权重（或加密/损坏）: %s" % str(e)[:80])
        return info
    if not isinstance(d, dict):
        info.update(ok=False, msg="内容是 %s，不是 RVC 声线模型（需要 dict）"
                    % type(d).__name__)
        return info

    w = d.get("weight")
    if not isinstance(w, dict) or not w:
        info.update(ok=False,
                    msg="缺 weight 字段——这更像普通训练检查点/ComfyUI 等其他模型的 pth，不是 RVC 声线")
        return info
    rvc_layers = sum(1 for k in w if str(k).startswith("enc_p."))
    l2cs = sum(1 for k in w if str(k).startswith("l2cs."))   # caer 声线转换也常见
    info["params"] = len(w)
    if not rvc_layers:
        info.update(ok=False,
                    msg="权重里没有 RVC 特征层（enc_p.*），是别的模型（如 %s）"
                        % (str(list(w.keys())[:2])))
        return info

    sr = _norm_sr(d.get("sr"))
    f0 = _norm_f0(d.get("f0"))
    version = d.get("version")
    version = version.strip().lower() if isinstance(version, str) else None
    if version not in ("v1", "v2"):
        version = None
    epoch = d.get("epoch")
    if not isinstance(epoch, int):
        epoch = None
    note = d.get("info") or d.get("model_name") or ""
    if epoch is None and note:
        m = re.search(r"(\d+)\s*epoch", str(note), re.I)
        epoch = int(m.group(1)) if m else None

    warns = []
    if sr is None:
        warns.append("采样率缺失（个别老模型会出现，装上后若报错请删除）")
    if version is None:
        warns.append("版本号缺失（按 v1/v2 兼容处理）")
    if f0 is None:
        warns.append("f0 标记缺失")

    info.update(ok=True,
                sr=sr, f0=f0, version=version, epoch=epoch,
                author=(str(d.get("author"))[:40] if d.get("author") else ""),
                embedder=(str(d.get("embedder_model"))[:40]
                          if d.get("embedder_model") else ""),
                note=str(note)[:80],
                warns=warns,
                msg="RVC 声线模型")
    return info


def index_base(name):
    """从索引文件名提取「模型基名」：feiyu_added_v2 → feiyu。

    RVC 索引命名五花八门（经典 RVC / Applio 两派），把已知装饰词剥掉，
    相邻重复词再去一次重（Applio 会把模型名写两遍）。
    """
    stem = os.path.splitext(os.path.basename(str(name)))[0].lower()
    stem = re.sub(r"added|ivf\d+|flat|nprobe_?\d+|v[12]", " ", stem)
    words = [w for w in re.split(r"[^0-9a-z\u4e00-\u9fff]+", stem) if w]
    out = []
    for w in words:
        if not out or out[-1] != w:
            out.append(w)
    return "".join(out)


def inspect_index(path):
    """验一个 .index 是不是 faiss 索引（RVC 索引都是 faiss 存的）。"""
    info = {"path": path, "name": os.path.basename(path), "type": "index"}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        info.update(ok=False, msg="读不到文件: %s" % e)
        return info
    info["mb"] = round(size / 1048576, 2)
    if size < MIN_INDEX_BYTES:
        info.update(ok=False, msg="文件太小（%d B），不是有效索引" % size)
        return info
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError as e:
        info.update(ok=False, msg="读不到文件: %s" % e)
        return info
    # faiss 索引魔数：IwFl（IVF 类）/ IxfI（Flat）等都以 Iw/Ix 开头
    if head[:2] not in (b"Iw", b"Ix"):
        info.update(ok=False,
                    msg="不是 faiss 索引（头 %r）——RVC 的 .index 都是 faiss 格式"
                        % head[:4])
        return info
    info.update(ok=True, base=index_base(path), msg="faiss 索引")
    return info


def check(paths):
    """批量识别 + 自动配对。返回 {"ok", "items", "pairs"}。"""
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "msg": "没有要识别的文件"}
    if len(paths) > MAX_FILES:
        return {"ok": False, "msg": "一次最多识别 %d 个文件" % MAX_FILES}
    items = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pth":
            items.append(inspect_pth(p))
        elif ext == ".index":
            items.append(inspect_index(p))
        else:
            items.append({"path": p, "name": os.path.basename(p), "type": "?",
                          "ok": False,
                          "msg": "不认识的文件类型（只支持 .pth 和 .index）"})
    if not items:
        return {"ok": False, "msg": "没有可识别的文件"}

    # 自动配对：每个 pth 找最合适的 index（基名互相包含优先，其次相似度）
    idxs = [x for x in items if x.get("type") == "index" and x.get("ok")]
    pairs = []
    for it in items:
        if it.get("type") != "pth" or not it.get("ok"):
            continue
        pbase = os.path.splitext(it["name"])[0].lower()
        pbase = index_base(pbase) or pbase
        best, score = None, 0.0
        for ix in idxs:
            ib = ix.get("base") or ""
            s = 1.0 if (ib and (ib in pbase or pbase in ib)) else 0.0
            if s < 1.0:
                try:
                    from difflib import SequenceMatcher
                    s = SequenceMatcher(None, pbase, ib).ratio()
                except Exception:
                    s = 0.0
            if s > score:
                best, score = ix, s
        if best and score >= 0.5:
            it["match_index"] = best["path"]
            it["match_score"] = round(score, 2)
            pairs.append({"pth": it["name"], "index": best["name"]})
        else:
            it["match_index"] = None
    return {"ok": True, "items": items, "pairs": pairs}


def install(pth, index=None, replace=False, name=None, index_name=None):
    """把选好的模型装进 WebUI。目标位置固定，文件名净化后落盘。

    name / index_name 可选：指定装进去的文件名（模型市场下载的缓存文件带仓库前缀，
    装的时候要用仓库里的原始文件名，不然声线列表里全是 u1__repo__xxx.pth）。
    """
    out = {"ok": False, "pth": "", "index": "", "replace": bool(replace)}
    if not isinstance(pth, str) or not pth.strip():
        out["msg"] = "缺少 .pth 文件路径"
        return out
    src_pth = pth.strip()
    if not os.path.isfile(src_pth) or os.path.splitext(src_pth)[1].lower() != ".pth":
        out["msg"] = "pth 路径无效（必须是指定存在的 .pth 文件）"
        return out
    info = inspect_pth(src_pth)
    if not info.get("ok"):
        out["msg"] = "pth 校验未通过: %s" % info.get("msg")
        return out

    src_index = None
    if isinstance(index, str) and index.strip():
        src_index = index.strip()
        if not os.path.isfile(src_index) or \
                os.path.splitext(src_index)[1].lower() != ".index":
            out["msg"] = "index 路径无效（必须是指定存在的 .index 文件）"
            return out
        if not inspect_index(src_index).get("ok"):
            out["msg"] = "index 校验未通过: %s" % inspect_index(src_index).get("msg")
            return out

    name_pth = _clean_name(name or info["name"]) or "model.pth"
    dest_pth = os.path.join(WEIGHTS_DIR, name_pth)
    if os.path.abspath(dest_pth) != os.path.abspath(src_pth) and \
            os.path.exists(dest_pth) and not replace:
        out.update(msg="weights 里已有同名文件：%s（勾选「覆盖」再点导入可替换）" % name_pth,
                   exists=True, existing=name_pth)
        return out

    name_ix = ""
    dest_ix = None
    if src_index:
        base = os.path.splitext(name_pth)[0].lower()
        raw = _clean_name(index_name or os.path.basename(src_index))
        if base not in os.path.splitext(raw)[0].lower():
            # 索引名和模型名对不上就按模型名重命名，保证换声时能自动配对
            raw = "%s_added_v2.index" % base
        name_ix = raw
        dest_ix = os.path.join(INDEX_DIR, name_ix)
        if os.path.abspath(dest_ix) != os.path.abspath(src_index) and \
                os.path.exists(dest_ix) and not replace:
            out.update(msg="indices 里已有同名文件：%s（勾选「覆盖」再点导入可替换）" % name_ix,
                       exists=True, existing=name_ix)
            return out

    try:
        os.makedirs(WEIGHTS_DIR, exist_ok=True)
        if os.path.abspath(dest_pth) != os.path.abspath(src_pth):
            shutil.copy2(src_pth, dest_pth)
        if dest_ix is not None and \
                os.path.abspath(dest_ix) != os.path.abspath(src_index):
            os.makedirs(INDEX_DIR, exist_ok=True)
            shutil.copy2(src_index, dest_ix)
    except Exception as e:
        out["msg"] = "复制失败: %s" % e
        return out

    # 落盘后复核：大小一致才算成功
    if os.path.getsize(dest_pth) != os.path.getsize(src_pth):
        out["msg"] = "复制后大小不一致，安装结果可疑，请删除后重试"
        return out
    if dest_ix is not None and \
            os.path.getsize(dest_ix) != os.path.getsize(src_index):
        out["msg"] = "索引复制后大小不一致，请删除后重试"
        return out

    out.update(ok=True, pth=name_pth, index=name_ix,
               weights_dir=WEIGHTS_DIR,
               msg="已安装 %s%s" % (name_pth,
                                    ("（索引 %s）" % name_ix) if name_ix else ""))
    return out


def handle_post(path, body):
    """panel.py 的 /api/admin/import_* 统一入口。"""
    body = body if isinstance(body, dict) else {}
    try:
        if path == "/api/admin/import_pick":
            return 200, pick_files()
        if path == "/api/admin/import_check":
            return 200, check(body.get("paths"))
        if path == "/api/admin/import_install":
            return 200, install(body.get("pth"), body.get("index"),
                                bool(body.get("replace")))
    except Exception as e:
        return 500, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    return 404, {"ok": False, "msg": "not found"}
