# -*- coding: utf-8 -*-
"""把两个 venv 的 pyvenv.cfg 指向「本盘」的内置 Python 运行时。

盘符变了（E: 插到别的机器上变成 F:）就跑一次这个脚本，
它会按自己所在位置推导根目录，重写 home / executable。
用法：python fix_paths.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))      # <root>\runtime
ROOT = os.path.dirname(HERE)                           # <root>
PY_HOME = os.path.join(HERE, "python312")
VENVS = [
    os.path.join(ROOT, "RVCSVC", "apienv"),
    os.path.join(ROOT, "RVC-WebUI", ".venv"),
]
TEMPLATE = (
    "home = {home}\n"
    "include-system-site-packages = false\n"
    "version = 3.12.10\n"
    "executable = {exe}\n"
)


def main():
    if not os.path.isfile(os.path.join(PY_HOME, "python.exe")):
        print("[X] 没找到内置运行时: %s" % PY_HOME)
        return 2
    print("根目录: %s" % ROOT)
    print("运行时: %s" % PY_HOME)
    for v in VENVS:
        cfg = os.path.join(v, "pyvenv.cfg")
        if not os.path.isdir(v):
            print("  跳过(无此 venv) %s" % v)
            continue
        text = TEMPLATE.format(home=PY_HOME, exe=os.path.join(PY_HOME, "python.exe"))
        with open(cfg, "w", encoding="ascii", newline="\n") as f:
            f.write(text)
        print("  已改写 %s" % cfg)
        print("    -> %s" % text.splitlines()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
