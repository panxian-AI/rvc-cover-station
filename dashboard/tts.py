# -*- coding: utf-8 -*-
"""文字转语音（TTS），挂在面板里，与翻唱共用审批 / 成品库 / 下载通道。

两种模式：
  1. 系统音色（edge-tts 直出）—— 11 个微软音色，几秒到半分钟
  2. 我的声线（先 TTS，再套 RVC）—— edge-tts 合成底声 → 后端 /convert_voice_file
     换成 assets/weights 里任意一个 .pth 的音色，得到「自己的声音在读」

流程与翻唱一致：
    访客提交（文字 + 音色 + 语速）→ 站长批准 → 合成/换声 → 进成品库 → 访客下载
成品和翻唱一样收进 covers 目录、登记进 index.json，
所以访客下载走的就是同一条 /api/guest/file 通道。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
import time
import traceback

from paths import COVERS_DIR  # noqa: E402

MAX_TEXT = 1200                 # 单次合成字数上限
MAX_JOBS = 40
RVC_PREFIX = "rvc:"             # 音色 id 前缀：rvc:feiyu.pth
API_BASE = "http://127.0.0.1:3333"
DEFAULT_BASE = "zh-CN-XiaoxiaoNeural"   # 换声时的默认底声（女声）

# 精选音色（label 给前台显示，id 是 edge-tts 的 ShortName）
VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural",          "label": "晓晓 · 女·温柔（普通话）"},
    {"id": "zh-CN-XiaoyiNeural",            "label": "晓伊 · 女·活泼（普通话）"},
    {"id": "zh-CN-YunxiNeural",             "label": "云希 · 男·年轻（普通话）"},
    {"id": "zh-CN-YunjianNeural",           "label": "云健 · 男·浑厚（普通话）"},
    {"id": "zh-CN-YunyangNeural",           "label": "云扬 · 男·播音（普通话）"},
    {"id": "zh-CN-liaoning-XiaobeiNeural",  "label": "小贝 · 女·东北话"},
    {"id": "zh-TW-HsiaoChenNeural",         "label": "曉臻 · 女（台湾国语）"},
    {"id": "zh-HK-HiuMaanNeural",           "label": "曉曼 · 女（粤语）"},
    {"id": "ja-JP-NanamiNeural",            "label": "七海 · 女（日语）"},
    {"id": "en-US-AriaNeural",              "label": "Aria · 女（英语）"},
    {"id": "en-US-GuyNeural",               "label": "Guy · 男（英语）"},
]
VOICE_IDS = {v["id"] for v in VOICES}
# 换声只认中文底声（外语底声换中文声线会串味）
BASE_VOICES = [v for v in VOICES if v["id"].startswith("zh-CN")]
BASE_IDS = {v["id"] for v in BASE_VOICES}
RATES = {"慢": "-25%", "正常": "+0%", "快": "+30%"}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


# ---------------- 声线（RVC 模型） ----------------
def weights_dir() -> str:
    from cover import WEIGHTS_DIR
    return WEIGHTS_DIR


def model_label(name: str) -> str:
    try:
        from cover import MODEL_LABELS
        return MODEL_LABELS.get(name.lower(), os.path.splitext(name)[0])
    except Exception:
        return os.path.splitext(name)[0]


def rvc_voices() -> list:
    """weights 目录里的声线，包成 TTS 可选音色（id = rvc:<文件名>）。"""
    out = []
    try:
        d = weights_dir()
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".pth"):
                out.append({"id": RVC_PREFIX + f, "label": model_label(f)})
    except Exception:
        pass
    return out


def _valid_model(name: str) -> str:
    """只接受 weights 目录里真实存在的 .pth，挡掉路径穿越。"""
    name = os.path.basename(str(name or "").strip())
    if not name.lower().endswith(".pth"):
        return ""
    try:
        return name if os.path.isfile(os.path.join(weights_dir(), name)) else ""
    except Exception:
        return ""


# ---------------- 任务队列 ----------------
def _new_job(**kw) -> str:
    import uuid
    jid = uuid.uuid4().hex[:12]
    with _lock:
        if len(_jobs) > MAX_JOBS:
            done = sorted((j for j in _jobs.items() if j[1]["state"] in ("done", "error")),
                          key=lambda x: x[1]["created"])
            for k, _v in done[: len(_jobs) - MAX_JOBS]:
                _jobs.pop(k, None)
        _jobs[jid] = {"state": "queued", "stage": "排队中", "created": time.time(),
                      "started": None, "ended": None, "file": None, "error": None,
                      "title": kw.get("title"), "model": kw.get("model") or kw.get("voice"),
                      "key_shift": None, "acapella": False}
    return jid


def _update(jid, **kw):
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def job_status(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return None
        out = dict(j)
    out["elapsed"] = round((out["ended"] or time.time()) - out["started"], 1) if out["started"] else 0
    return out


def voices() -> dict:
    return {"ok": True, "voices": VOICES, "rvc": rvc_voices(),
            "bases": BASE_VOICES, "rates": list(RATES.keys()), "max_text": MAX_TEXT}


# ---------------- 校验 / 启动 ----------------
def validate(payload: dict) -> dict:
    """校验一条朗读申请，返回归一化后的参数（不真正起任务）。"""
    text = str(payload.get("text") or "").strip()
    voice = str(payload.get("voice") or "").strip()
    rate = str(payload.get("rate") or "正常").strip()
    base = str(payload.get("base") or DEFAULT_BASE).strip()
    if not text:
        return {"ok": False, "msg": "请输入要朗读的文字"}
    if len(text) > MAX_TEXT:
        return {"ok": False, "msg": "文字太长，一次最多 %d 字" % MAX_TEXT}
    if rate not in RATES:
        rate = "正常"

    model = ""
    if voice.startswith(RVC_PREFIX):
        model = _valid_model(voice[len(RVC_PREFIX):])
        if not model:
            return {"ok": False, "msg": "这条声线已经不在硬盘上了，换一个试试"}
        if base not in BASE_IDS:
            base = DEFAULT_BASE
        voice = RVC_PREFIX + model        # 归一化，别把 ../ 之类原样存进库
        nice = model_label(model)
    elif voice in VOICE_IDS:
        base = voice
        nice = voice.replace("Neural", "")
    else:
        return {"ok": False, "msg": "请选择一个音色"}

    return {"ok": True, "text": text, "voice": voice, "base": base,
            "rate": rate, "model": model, "label": nice,
            "title": re.sub(r"\s+", " ", text[:16]).strip() or "朗读"}


def start(payload: dict) -> dict:
    """校验并排队一个 TTS 任务；同样内容做过就直接复用成品。"""
    v = validate(payload)
    if not v.get("ok"):
        return {"ok": False, "msg": v.get("msg", "参数不合法")}

    tkey = text_key(v["text"], v["voice"], v["base"], v["rate"])
    hit = _find_cached(tkey)
    if hit:
        jid = _new_job(title=v["title"], voice=v["base"], model=v["model"])
        now = time.time()
        _update(jid, state="done", started=now, ended=now,
                stage="复用已有成品（没有重复合成）", file=hit["name"],
                size=hit["size"], size_text=human_size(hit["size"]), cached=True)
        return {"ok": True, "job": jid, "cached": True, "file": hit["name"],
                "msg": "这段文字这个音色之前做过，直接给你现成的"}

    jid = _new_job(title=v["title"], voice=v["base"], model=v["model"])
    threading.Thread(target=_run, args=(jid, dict(v, tkey=tkey)), daemon=True).start()
    return {"ok": True, "job": jid,
            "msg": "已开始换声，一般半分钟到一分钟" if v["model"]
                   else "已开始合成，一般十几秒到一分钟"}


# ---------------- 成品复用 ----------------
def text_key(text: str, voice: str, base: str, rate: str) -> str:
    """内容指纹：文字 + 音色 + 底声 + 语速。同样四样的朗读只做一次。"""
    import hashlib
    raw = "%s\x00%s\x00%s\x00%s" % (str(text or ""), str(voice or ""),
                                    str(base or ""), str(rate or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _find_cached(tkey: str) -> dict | None:
    """在成品库里找同指纹、文件还在的朗读成品（新的优先）。"""
    if not tkey:
        return None
    from cover import COVERS_DIR
    for it in reversed(cover_index()):
        if it.get("tts_key") != tkey:
            continue
        p = os.path.join(COVERS_DIR, os.path.basename(str(it.get("name") or "")))
        try:
            sz = os.path.getsize(p) if os.path.isfile(p) else 0
        except OSError:
            sz = 0
        if sz <= 1024:
            continue        # 记录还在文件没了，当作没做过
        return {"name": os.path.basename(p), "size": sz, "size_text": human_size(sz)}
    return None


# ---------------- 合成 / 换声 ----------------
def _work_dir() -> str:
    d = os.path.join(os.path.dirname(COVERS_DIR), "_tts_work")
    os.makedirs(d, exist_ok=True)
    # 顺手清掉一天前的残留（任务中断/失败时留下的底声）
    try:
        cut = time.time() - 86400
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p) and os.path.getmtime(p) < cut:
                os.remove(p)
    except OSError:
        pass
    return d


def _synth(text: str, base: str, rate: str, jid: str) -> str:
    import edge_tts

    out = os.path.join(_work_dir(), "_tts_%s.mp3" % jid)

    async def run():
        await edge_tts.Communicate(text, base, rate=RATES[rate]).save(out)

    asyncio.run(run())
    if not os.path.isfile(out) or os.path.getsize(out) < 200:
        raise RuntimeError("合成结果为空，稍后再试")
    return out


def _port_open(port: int, host="127.0.0.1", timeout=0.8) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _to_rvc(src: str, model: str, jid: str) -> str:
    """把合成好的底声送给后端换成目标声线（单文件推理，不分离不加混响）。"""
    if not _port_open(3333):
        raise RuntimeError("后端 3333 没在运行，请先在面板里「一键全部启动」")
    from gradio_client import Client

    c = Client(API_BASE + "/", verbose=False)
    res = c.predict(
        audio_path=src, model_dropdown=model, f0_method="rmvpe",
        index_rate="0.75", protect="0.33", rms_mix_rate="0.0",
        resample_sr="0", key_shift="0",
        api_name="/convert_voice_file",
    )
    if not res or not os.path.isfile(str(res)):
        raise RuntimeError("后端没有返回换声结果：%r" % (res,))
    return str(res)


# ---------------- 归档 ----------------
def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(s or "")).strip(" .")
    return s or "未命名"


def _archive(src: str, title: str, label: str, model: str, tkey: str = "") -> str:
    """收进 covers 目录并登记（复用翻唱的 index.json）。"""
    from cover import trusted_src
    if not trusted_src(src):
        raise RuntimeError("换声结果不在允许的临时目录内，已拒绝处理：%s" % src)
    os.makedirs(COVERS_DIR, exist_ok=True)
    stem = "朗读-" + _safe_name(title)[:40] + "-" + _safe_name(label)[:32]
    name = stem + ".mp3"
    n = 2
    while os.path.exists(os.path.join(COVERS_DIR, name)):
        name = "%s(%d).mp3" % (stem, n)
        n += 1
    dst = os.path.join(COVERS_DIR, name)
    shutil.copy2(src, dst)
    for p in (src,):
        try:
            os.remove(p)
        except OSError:
            pass
    items = cover_index()
    items.append({"name": name, "title": "朗读：" + title, "artist": label,
                  "model": model or label, "key_shift": None, "acapella": False,
                  "song_id": "tts", "created": time.time(),
                  "tts_key": tkey or "",
                  "size": os.path.getsize(dst)})
    save_index(items)
    return name


def human_size(n) -> str:
    """与成品库的显示格式化保持一致（cover.human_size）。"""
    try:
        from cover import human_size as _hs
        return _hs(n)
    except Exception:
        return "%.1f MB" % (float(n or 0) / 1048576)


def cover_index():
    """读成品索引：直接复用 cover 的实现（同一把锁，避免和翻唱互相覆盖）。"""
    try:
        from cover import _load_index
        return _load_index()
    except Exception:
        return []


def save_index(items):
    """写成品索引：同上，走 cover 的加锁版本。"""
    from cover import _save_index
    _save_index(items)


def json_load(f):
    import json
    return json.load(f)


def json_dump(items):
    import json
    return json.dumps(items, ensure_ascii=False, indent=1)


def _run(jid, v: dict):
    _update(jid, state="running", started=time.time(), stage="连接语音服务…")
    try:
        text, base, rate = v["text"], v["base"], v["rate"]
        model, label, title = v["model"], v["label"], v["title"]
        _update(jid, stage="合成底声（%d 字）…" % len(text))
        mp3 = _synth(text, base, rate, jid)
        if model:
            _update(jid, stage="换成「%s」的音色…" % label)
            raw = mp3
            mp3 = _to_rvc(raw, model, jid)
            try:
                os.remove(raw)          # 底声已无用，别在 _tts_work 里堆着
            except OSError:
                pass
        _update(jid, stage="归档…")
        name = _archive(mp3, title, label, model, v.get("tkey") or "")
        sz = os.path.getsize(os.path.join(COVERS_DIR, name))
        _update(jid, state="done", stage="完成", ended=time.time(), file=name,
                size=sz, size_text="%.1f MB" % (sz / 1048576))
    except Exception as e:
        _update(jid, state="error", stage="失败", ended=time.time(),
                error="%s: %s" % (type(e).__name__, e))
        traceback.print_exc()
