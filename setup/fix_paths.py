# -*- coding: utf-8 -*-
"""把 venv 的 pyvenv.cfg 指向「本盘」的内置 Python 运行时。

盘符变了（E: 插到别的机器上变成 F:）、安装到别的目录、换个文件夹名，
都可以跑一次这个脚本——它自己找运行时，不假设自己在哪一层。

用法：python fix_paths.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

TEMPLATE = (
    "home = {home}\n"
    "include-system-site-packages = false\n"
    "version = 3.12.10\n"
    "executable = {exe}\n"
)


def find_root():
    """从脚本所在位置逐层往上找「含 runtime\\python312\\python.exe」的目录。

    为什么不写死 <root>\\runtime：安装器会把 setup 目录搬到
    <root>\\RVCSVC\\setup，同一个脚本放在两个位置，上一层的含义完全不同。
    以前按「我就在 runtime 下」推导，从 setup 跑就会算出
    <root>\\RVCSVC\\runtime 这种不存在的地方，然后报「没找到内置运行时」。
    """
    cands = []
    cur = HERE
    for _ in range(4):
        cands.append(cur)
        cands.append(os.path.join(cur, "RVC-Data"))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for c in cands:
        if os.path.isfile(os.path.join(c, "runtime", "python312", "python.exe")):
            return c
    return None


def find_venvs(root):
    """装了哪几个 venv 就改哪几个，路径也按常见布局自己找"""
    cands = [
        os.path.join(root, "RVCSVC", "apienv"),
        os.path.join(root, "RVC-WebUI", ".venv"),
        os.path.join(root, "apienv"),
        os.path.join(root, "RVC-Data", "RVCSVC", "apienv"),
    ]
    seen, out = set(), []
    for p in cands:
        rp = os.path.abspath(p)
        if rp in seen:
            continue
        seen.add(rp)
        if os.path.isdir(rp):
            out.append(rp)
    return out


def main():
    root = find_root()
    if not root:
        print("[X] 没找到内置运行时（runtime\\python312\\python.exe）")
        print("    从这个位置往上找了 4 层，都没有：")
        print("    脚本位置: %s" % HERE)
        print("    如果安装目录被移动过，请重跑一次 一键安装.bat（可断点续装）。")
        return 2

    py_home = os.path.join(root, "runtime", "python312")
    print("根目录: %s" % root)
    print("运行时: %s" % py_home)

    venvs = find_venvs(root)
    if not venvs:
        print("[!] 没找到任何 venv（apienv / .venv），跳过")
        return 0

    text = TEMPLATE.format(home=py_home, exe=os.path.join(py_home, "python.exe"))
    for v in venvs:
        cfg = os.path.join(v, "pyvenv.cfg")
        try:
            with open(cfg, "w", encoding="ascii", newline="\n") as f:
                f.write(text)
        except OSError as e:
            print("  [X] 写不了 %s: %s" % (cfg, e))
            return 3
        print("  已改写 %s" % cfg)
        print("    -> home = %s" % py_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
