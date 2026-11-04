# -*- coding: utf-8 -*-
"""依赖版本核对。

为什么要有这个：面板层用的东西不多，但都是「上游一升级就变行为」的那类
（edge-tts 改接口、gradio-client 改字段、Pillow 改 API）。锁死版本只解决一半
问题——机器上装的可能早就不是锁的那个版本了（手动 pip 升过、装别的包带上来
的）。所以启动时对一遍账，控制台直接告诉你差在哪。

纯标准库；拿不到版本就报 unknown，绝不影响面板启动。
"""
import importlib.metadata as _md
import os
import re
import threading
import time

REQ_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "requirements.txt")

_lock = threading.Lock()
_cache = {"t": 0.0, "v": None}
_TTL = 300                      # 5 分钟；依赖不会在运行中变，除非有人手动装

# 包名 → importlib.metadata 里的正式名（写 requirements 时常用短名）
_ALIAS = {
    "pillow": "pillow",
    "edge-tts": "edge-tts",
    "gradio-client": "gradio-client",
}


def normalize(name):
    return re.sub(r"[-_.]+", "-", str(name or "").strip().lower())


def parse(path=None):
    """读 requirements.txt → [{name, spec, op, want}]，只认锁死的 == / >= / ~=。"""
    p = path or REQ_FILE
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        # 允许行内注释、环境标记（; python_version<"3.9"）与 extras（pkg[extra]==x）
        line = line.split(";")[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(==|>=|<=|~=|>|<)?\s*([^\s,]+)?", line)
        if not m:
            continue
        name = m.group(1)
        op = m.group(3) or ""
        want = (m.group(4) or "").strip()
        out.append({"name": name, "key": normalize(name), "op": op, "want": want})
    return out


def have_version(name):
    key = _ALIAS.get(normalize(name), normalize(name))
    try:
        return _md.version(key)
    except Exception:
        return ""


def _cmp_tuple(v):
    parts = re.findall(r"\d+", str(v) or "")
    return tuple(int(x) for x in parts[:4]) or (0,)


def check(force=False, path=None):
    """比对「锁的版本」和「装的版本」。

    返回 drift 列表：缺包、装错版本、多装了都不算致命，但要让人看见——
    面板照样启动，只是控制台会挂一条黄色提示。
    """
    now = time.time()
    with _lock:
        if not force and _cache["v"] is not None and now - _cache["t"] < _TTL:
            return _cache["v"]
    items = []
    for it in parse(path):
        got = have_version(it["name"])
        status = "ok"
        if not got:
            status = "missing"
        elif it["op"] == "==" and it["want"] and _cmp_tuple(got) != _cmp_tuple(it["want"]):
            status = "diff"
        elif it["op"] == ">=" and it["want"] and _cmp_tuple(got) < _cmp_tuple(it["want"]):
            status = "diff"
        elif it["op"] == "~=" and it["want"] and not got.startswith(it["want"].rsplit(".", 1)[0]):
            status = "diff"
        items.append({"name": it["name"], "want": (it["op"] + it["want"]).strip(),
                      "have": got or "未安装", "status": status})
    drift = [x for x in items if x["status"] != "ok"]
    if not items:
        # 以前这里返回 ok=True 并写「跳过核对」：结果清单文件没被安装时，
        # 整个「依赖锁」在用户机器上是静默空转的，谁也不会发现。
        res = {"ok": False, "file": path or REQ_FILE, "items": [], "drift": [],
               "missing_file": True,
               "msg": "没找到 requirements.txt（依赖版本没有核对）", "checked": now}
    elif not drift:
        res = {"ok": True, "file": path or REQ_FILE, "items": items, "drift": [],
               "msg": "%d 项依赖与锁定版本一致" % len(items), "checked": now}
    else:
        res = {"ok": False, "file": path or REQ_FILE, "items": items, "drift": drift,
               "msg": "%d 项依赖和锁定版本不一致：%s" % (
                   len(drift), "、".join("%s(%s)" % (x["name"], x["have"]) for x in drift[:4])),
               "checked": now}
    with _lock:
        _cache["t"], _cache["v"] = now, res
    return res


def repair_cmd(path=None):
    """一条能直接粘到命令行的修复命令（用当前解释器，别指望 PATH 里的 python）。"""
    import sys
    p = path or REQ_FILE
    return '"%s" -m pip install -r "%s"' % (sys.executable, p)


if __name__ == "__main__":
    import json
    import sys
    r = check(force=True)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if r.get("ok") else 1)
