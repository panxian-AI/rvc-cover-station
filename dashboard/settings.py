# -*- coding: utf-8 -*-
"""高级设置（站长可调的翻唱参数 + 并发数）。

为什么单独一个模块：
    翻唱参数原来写死在 cover.py 的 predict 调用里，队列并发写死在 guest.py
    的「一次推进一单」里，想调只能改代码。这里集中成一份可持久化、带校验、
    带默认值的配置，控制台页面直接读写。

设计要点：
    · 落盘 dashboard/advanced_settings.json（和 guest_db.json 同目录）；
    · 所有字段走 SPEC 声明式校验：类型收窄、数值夹到区间、枚举外的值打回默认；
    · save() 是「合并补丁」语义：只传要改的字段，其余保持原样；
    · 读的都是内存缓存，翻唱任务跑一次读一次，改设置即时生效（不用重启）。
"""
from __future__ import annotations

import json
import os
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认和 guest_db.json 同目录；测试实例用 RVC_SETTINGS_PATH 指到临时文件，
# 免得跑测试把站长真在用的设置改掉
PATH = os.environ.get("RVC_SETTINGS_PATH") or os.path.join(HERE, "advanced_settings.json")

_lock = threading.RLock()
_cache: dict | None = None

# 字段声明。顺序即控制台卡片里的显示顺序。
#   key/默认值/类型/范围或枚举/界面文案/给站长的一句话说明
SPEC = [
    {"key": "max_parallel", "default": 1, "type": "int", "min": 1, "max": 6,
     "label": "同时制作数量",
     "hint": "一次最多同时跑几个翻唱任务。1=排队逐个来（最稳），调大可以并行做多单，"
             "但每一路都会分走显卡算力，单首会更慢；显存小（<8G）建议保持 1~2。"},
    {"key": "f0_method", "default": "rmvpe", "type": "enum",
     "choices": ["rmvpe", "crepe", "harvest", "pm"],
     "label": "音高算法",
     "hint": "rmvpe=推荐，又快又稳；crepe=更准但慢很多；harvest=老算法；pm=最快但音准差。"},
    {"key": "index_rate", "default": 0.75, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05,
     "label": "声线检索强度",
     "hint": "0=只靠模型本体，更像模型；1=最大程度往索引音色靠。一般 0.6~0.8，调太高容易出电流杂音。"},
    {"key": "filter_radius", "default": 3, "type": "int", "min": 0, "max": 7,
     "label": "音高滤波半径",
     "hint": "对音高曲线做平滑，0=不平滑。默认 3 就行，数值大音高更稳但咬字会僵。"},
    {"key": "reverb_intensity", "default": 4.0, "type": "float", "min": 0.0, "max": 10.0, "step": 0.5,
     "label": "混响强度",
     "hint": "0=干声（录音棚感），10=大厅感。默认 4。"},
    {"key": "delay_intensity", "default": 0.0, "type": "float", "min": 0.0, "max": 10.0, "step": 0.5,
     "label": "回声强度",
     "hint": "跟在节拍后面的延迟回声，0=关。喜欢空灵效果可以加到 2~4。"},
    {"key": "uvr5_agg", "default": 10, "type": "int", "min": 0, "max": 20,
     "label": "人声分离聚合度",
     "hint": "越高人声越「干净」、伴奏剔得越狠，太高会伤人声质感。默认 10。"},
    {"key": "vocal_vol", "default": 0.0, "type": "float", "min": -10.0, "max": 10.0, "step": 0.5,
     "label": "人声音量（dB）",
     "hint": "成品里人声相对伴奏的音量增减，0=不调整。人声被伴奏盖住就加 1~3。"},
    {"key": "inst_vol", "default": 0.0, "type": "float", "min": -10.0, "max": 10.0, "step": 0.5,
     "label": "伴奏音量（dB）",
     "hint": "伴奏相对人声的音量增减，0=不调整。注意压到 -40 以下会变成清唱模式。"},
]

_BY_KEY = {s["key"]: s for s in SPEC}


def _coerce(spec: dict, raw) -> tuple[object, list[str]]:
    """把任意输入收窄成合法值。返回 (值, 警告列表)。"""
    warns: list[str] = []
    if spec["type"] == "enum":
        v = str(raw or "").strip().lower()
        if v in spec["choices"]:
            return v, warns
        warns.append("%s 不是合法选项，已恢复为 %s" % (spec["label"], spec["default"]))
        return spec["default"], warns
    if spec["type"] == "int":
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            warns.append("%s 不是数字，已恢复为 %s" % (spec["label"], spec["default"]))
            return spec["default"], warns
        lo, hi = spec["min"], spec["max"]
        if v < lo or v > hi:
            fixed = max(lo, min(hi, v))
            warns.append("%s 超出范围（%d~%d），已夹到 %d" % (spec["label"], lo, hi, fixed))
            v = fixed
        return v, warns
    # float
    try:
        v = float(raw)
    except (TypeError, ValueError):
        warns.append("%s 不是数字，已恢复为 %s" % (spec["label"], spec["default"]))
        return spec["default"], warns
    lo, hi = spec["min"], spec["max"]
    if v < lo or v > hi:
        fixed = max(lo, min(hi, v))
        warns.append("%s 超出范围（%g~%g），已夹到 %g" % (spec["label"], lo, hi, fixed))
        v = fixed
    return round(v, 3), warns


def _defaults() -> dict:
    return {s["key"]: s["default"] for s in SPEC}


def _sanitize(raw: dict) -> tuple[dict, list[str]]:
    """把读出来/提交上来的 dict 过一遍校验，返回 (干净值, 警告)。未知键忽略。"""
    vals, warns = _defaults(), []
    for s in SPEC:
        k = s["key"]
        if k in raw:
            v, w = _coerce(s, raw[k])
            vals[k] = v
            warns += w
    return vals, warns


def load() -> dict:
    """读盘（损坏 / 缺字段都兜回默认）。"""
    # 没有 global 时 `_cache = vals` 只写进局部作用域，模块级缓存永远是 None：
    # 于是每次 get() 都重新开文件读 JSON（排队时 _acquire_slot 每秒调一次）。
    global _cache
    with _lock:
        try:
            with open(PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
        vals, warns = _sanitize(raw)
        vals["_warns"] = warns      # 读盘时的坏值也提示一次
        _cache = vals
        return dict(vals)


def get() -> dict:
    with _lock:
        if _cache is None:
            return load()
        return dict(_cache)


def save(patch) -> dict:
    """合并保存一个补丁 dict。返回 {ok, values, warns, msg}。"""
    global _cache
    if not isinstance(patch, dict):
        return {"ok": False, "msg": "设置内容必须是 JSON 对象"}
    patch = {k: v for k, v in patch.items() if k in _BY_KEY}
    if not patch:
        return {"ok": False, "msg": "没有可识别的设置项（合法字段：%s）"
                                    % "、".join(s["key"] for s in SPEC)}
    with _lock:
        cur = get()
        cur.pop("_warns", None)
        merged = dict(cur)
        merged.update(patch)
        vals, warns = _sanitize(merged)
        vals.pop("_warns", None)
        try:
            tmp = PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(vals, f, ensure_ascii=False, indent=1)
            os.replace(tmp, PATH)
        except OSError as e:
            return {"ok": False, "msg": "保存失败：%s" % e}
        vals["_warns"] = warns
        _cache = vals
        out = dict(vals)
        changed = "、".join(_BY_KEY[k]["label"] for k in patch)
        msg = "已保存：%s" % changed
        if warns:
            msg += "（注意：" + "；".join(warns) + "）"
        return {"ok": True, "values": {k: v for k, v in out.items() if k != "_warns"},
                "warns": warns, "msg": msg}


def reset() -> dict:
    """恢复全部默认。"""
    return save(_defaults())


def ui() -> dict:
    """给控制台页面：当前值 + 字段描述 + 默认值。"""
    vals = get()
    vals.pop("_warns", None)
    fields = []
    for s in SPEC:
        f = {k: s[k] for k in ("key", "label", "hint", "type") if k in s}
        f["value"] = vals.get(s["key"], s["default"])
        f["default"] = s["default"]
        if "min" in s:
            f["min"], f["max"] = s["min"], s["max"]
        if "step" in s:
            f["step"] = s["step"]
        if "choices" in s:
            f["choices"] = s["choices"]
        fields.append(f)
    return {"ok": True, "values": vals, "fields": fields,
            "file": PATH, "defaults": _defaults()}


def concurrency() -> int:
    """同时制作数量（cover 池和 guest 队列都读这个）。"""
    return int(get().get("max_parallel") or 1)


def gen_params() -> dict:
    """翻唱推理参数（cover._run_cover 每单跑之前取一次，改设置对下一单生效）。"""
    g = get()
    return {k: g[k] for k in ("f0_method", "index_rate", "filter_radius",
                              "reverb_intensity", "delay_intensity", "uvr5_agg",
                              "vocal_vol", "inst_vol")}


def _strip_comments(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


if __name__ == "__main__":
    # 手动检查：python settings.py 打印当前生效值
    print(json.dumps(get(), ensure_ascii=False, indent=1))
