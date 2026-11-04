# -*- coding: utf-8 -*-
"""全量回归：一条命令跑完本目录下所有 e2e_*.py。

用法（务必用面板自己那份解释器，系统 python 缺 torch / PIL）：

    cd <安装目录>\\RVCSVC\\dashboard
    ..\\apienv\\Scripts\\python.exe run_e2e.py

参数：

    --list            只列出会跑哪些套，不执行
    --only a,b        只跑名字里含 a 或 b 的套（逗号分隔，不区分大小写）
    --repo <dir>      仓库根（在安装目录里跑时指过去；等价于 set RVC_REPO_DIR）
    --timeout 600     单套超时秒数（默认 1800）
    --strict          把「跳过」也算失败（CI / 发版前建议带上）
    -q, --quiet       不打每套一行，只打失败详情与汇总
    --no-log          不写 logs\\e2e_*.log
    --tail 120        失败时回显末尾多少行（默认 120）

判定规则（写死在这里，别再出现「看起来全绿」）：

    1. 每套的「通过 / 失败」必须能从它自己的汇总行解析出来；
       解析不出来按**失败**算，不按「跳过」算 —— 否则新增一套换了措辞就会被静默漏掉。
    2. 失败数 > 0 即失败，**不管进程退出码是不是 0**：
       e2e_advanced / e2e_dataio / e2e_market 等好几套的 main() 里失败也 return 0。
    3. 退出码非 0、超时、异常退出，一律失败。
    4. 找不到仓库根时，只跳过 e2e_ai_install（安装器回归）并醒目提示；
       `--strict` 下跳过同样算失败。要跑它：set RVC_REPO_DIR=<仓库根>

退出码：0 全过；1 有失败（或 --strict 下有跳过）；2 参数/环境错误。
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(__file__)

# 需要仓库根（父目录里有 一键安装.bat）才能跑的套；运行盘上默认没有
NEEDS_REPO = {"e2e_ai_install.py"}

# 汇总行三种写法，前两个是「通过 + 失败」，第三个是「通过 / 总数」
_SUM_PF = (
    re.compile(r"(\d+)\s*通过\s*/\s*(\d+)\s*失败"),
    re.compile(r"通过\s*(\d+)\s*项\s*[，,]\s*失败\s*(\d+)\s*项"),
)
_SUM_PT = re.compile(r"通过\s*(\d+)\s*/\s*(\d+)")
_RE_OK_LINE = re.compile(r"(?m)^\s*\[OK\]")
_RE_BAD_LINE = re.compile(r"(?m)^\s*\[(?:!!|XX|FAIL)\]")
# 套自己声明「这一趟没条件跑」（如 e2e_ai_install 找不到仓库根），必须可见且可门禁
_RE_SKIP_LINE = re.compile(r"(?m)^\s*跳过[：:]\s*(.+?)\s*$")


def find_repo_root():
    """找仓库根：光有 一键安装.bat 不够 —— 安装目录也有，会被误判。

    真仓库根的判据是 `install/ai_install.ps1` 存在（e2e_ai_install 要读它）。
    """
    env = os.environ.get("RVC_REPO_DIR")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    d = HERE
    for _ in range(5):
        if (os.path.isfile(os.path.join(d, "一键安装.bat"))
                and os.path.isfile(os.path.join(d, "install", "ai_install.ps1"))):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def parse_summary(out):
    """从一套的输出里取 (通过, 总数, 失败, 来源)；解析不出来返回 None。"""
    tail = out[-4000:]  # 只看末尾，避免把中间过程里的「通过」误当汇总
    for pat in _SUM_PF:
        hits = pat.findall(tail)
        if hits:
            p, f = hits[-1]
            p, f = int(p), int(f)
            return p, p + f, f, "汇总"
    hits = _SUM_PT.findall(tail)
    if hits:
        p, t = hits[-1]
        p, t = int(p), int(t)
        return p, t, max(0, t - p), "汇总"
    n_ok = len(_RE_OK_LINE.findall(out))
    n_bad = len(_RE_BAD_LINE.findall(out))
    if n_ok or n_bad:
        return n_ok, n_ok + n_bad, n_bad, "标记"
    return None


def discover(only):
    names = []
    for fn in sorted(os.listdir(HERE)):
        if not (fn.startswith("e2e_") and fn.endswith(".py")):
            continue
        if fn == SELF:
            continue
        if only and not any(k in fn.lower() for k in only):
            continue
        names.append(fn)
    return names


def preflight():
    """提醒解释器选错：缺 torch/PIL 时这几套必然红，别让人以为是回归。"""
    missing = []
    for mod, why in (("torch", "e2e_market / e2e_model_import"),
                     ("PIL", "e2e_privacy")):
        try:
            __import__(mod)
        except Exception:
            missing.append("%s（%s）" % (mod, why))
    if missing:
        print("[警告] 当前解释器缺 %s，相关套会失败。" % "、".join(missing))
        print("       请用面板自己那份：..\\apienv\\Scripts\\python.exe run_e2e.py")
        print("       当前解释器：%s\n" % sys.executable)


def run_one(name, timeout):
    path = os.path.join(HERE, name)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    root = find_repo_root()
    if root:
        env.setdefault("RVC_REPO_DIR", root)
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, path], cwd=HERE, env=env,
                           capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace") + \
              (r.stderr or b"").decode("utf-8", "replace")
        rc = r.returncode
    except subprocess.TimeoutExpired as e:
        part = b""
        for k in (e.stdout, e.stderr):
            if k:
                part += k if isinstance(k, bytes) else str(k).encode("utf-8", "replace")
        out = part.decode("utf-8", "replace") + \
            "\n[runner] 超过 %ss 未结束，已终止。" % timeout
        rc = -9
    except Exception as e:  # noqa: BLE001 - 起不来也算失败，别吞
        out = "[runner] 无法执行：%r" % (e,)
        rc = -1
    return out, rc, time.time() - t0


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, description="跑全部 e2e_*.py 并给出可信汇总")
    ap.add_argument("--list", action="store_true", help="只列出会跑哪些套")
    ap.add_argument("--only", default="", help="只跑名字里含这些词的套，逗号分隔")
    ap.add_argument("--timeout", type=int, default=1800, help="单套超时秒数")
    ap.add_argument("--strict", action="store_true", help="有跳过也算失败")
    ap.add_argument("-q", "--quiet", action="store_true", help="只打失败详情与汇总")
    ap.add_argument("--no-log", action="store_true", help="不写日志文件")
    ap.add_argument("--tail", type=int, default=120, help="失败时回显末尾行数")
    ap.add_argument("--repo", default="", help="仓库根（安装目录里跑时用它指过去，等价于设 RVC_REPO_DIR）")
    args = ap.parse_args(argv)

    if args.repo:
        os.environ["RVC_REPO_DIR"] = os.path.abspath(args.repo)

    only = [k.strip().lower() for k in args.only.split(",") if k.strip()]
    names = discover(only)
    if not names:
        print("没找到任何 e2e_*.py（目录：%s）" % HERE)
        return 2
    if args.list:
        print("共 %d 套（目录：%s）" % (len(names), HERE))
        for n in names:
            print("  " + n)
        return 0

    root = find_repo_root()
    skip = {}
    if not root:
        for n in names:
            if n in NEEDS_REPO:
                skip[n] = ("找不到仓库根（需要 install/ai_install.ps1）；"
                           "要跑安装器回归：set RVC_REPO_DIR=<仓库根>")

    print("解释器：%s" % sys.executable)
    print("目录　：%s" % HERE)
    print("仓库根：%s" % (root or "未找到 —— e2e_ai_install 将被跳过"))
    print("套数　：%d（跳过 %d）\n" % (len(names), len(skip)))
    preflight()

    results = []
    logs = []
    t_start = time.time()
    for i, name in enumerate(names, 1):
        if name in skip:
            results.append({"name": name, "p": 0, "t": 0, "f": 0, "rc": None,
                            "secs": 0.0, "src": "跳过", "out": "", "why": skip[name]})
            if not args.quiet:
                print("[%2d/%2d] %-22s   跳过：%s" % (i, len(names), name, skip[name]))
            continue
        if not args.quiet:
            sys.stdout.write("[%2d/%2d] %-22s 跑…" % (i, len(names), name))
            sys.stdout.flush()
        out, rc, secs = run_one(name, args.timeout)
        got = parse_summary(out)
        if got is None:
            p = t = f = -1
            src = "解析失败"
            m = _RE_SKIP_LINE.search(out) if rc == 0 else None
            if m:
                # 套自己声明跳过了（如运行盘上找不到仓库根）——算跳过，但必须显眼
                results.append({"name": name, "p": 0, "t": 0, "f": 0, "rc": rc,
                                "secs": secs, "src": "跳过", "out": out,
                                "why": m.group(1).strip()})
                logs.append((name, out))
                if not args.quiet:
                    print("\r[%2d/%2d] %-22s 跳过：%s"
                          % (i, len(names), name, m.group(1).strip()))
                continue
        else:
            p, t, f, src = got
        if rc != 0:
            # 进程层面失败：汇总说全过也不算数
            f = max(f, 1)
        results.append({"name": name, "p": p, "t": t, "f": f, "rc": rc,
                        "secs": secs, "src": src, "out": out, "why": ""})
        logs.append((name, out))
        mark = "OK " if (f == 0) else "FAIL"
        if not args.quiet:
            print("\r[%2d/%2d] %-22s %-4s %4s/%-4s %6.1fs rc=%s"
                  % (i, len(names), name, mark,
                     "-" if p < 0 else p, "-" if t < 0 else t, secs, rc))
        elif f:
            print("[%2d/%2d] %-22s FAIL %s" % (i, len(names), name, src))

    # ---- 汇总 ----
    bad = [r for r in results if r["f"] != 0]
    skipped = [r for r in results if r["src"] == "跳过"]
    total_items = sum(r["t"] for r in results if r["t"] > 0)
    total_pass = sum(r["p"] for r in results if r["p"] > 0)
    dur = time.time() - t_start

    print("\n" + "=" * 66)
    notes = []
    for r in results:
        if r["src"] == "跳过":
            notes.append("  SKIP  %-22s %s" % (r["name"], r["why"]))
        elif r["f"] != 0:
            notes.append("  FAIL  %-22s %s/%s（失败 %s，来源：%s，rc=%s）"
                         % (r["name"],
                            "-" if r["p"] < 0 else r["p"],
                            "-" if r["t"] < 0 else r["t"], r["f"], r["src"], r["rc"]))
    for line in notes:
        print(line)
    if notes:
        print("=" * 66)
    print("套数 %d（跳过 %d）· 断言 %d 项 · 通过 %d · 失败套数 %d · 用时 %.1fs"
          % (len(results), len(skipped), total_items, total_pass, len(bad), dur))
    if bad:
        print("RESULT: FAIL")
    elif skipped:
        print("RESULT: PASS（有 %d 套被跳过）" % len(skipped))
    else:
        print("RESULT: PASS")

    if bad:
        print("\n" + "-" * 66)
        print("失败详情（末尾 %d 行；完整输出见日志）" % args.tail)
        for r in bad:
            print("\n---- %s（%s/%s，rc=%s）----"
                  % (r["name"],
                     "-" if r["p"] < 0 else r["p"],
                     "-" if r["t"] < 0 else r["t"], r["rc"]))
            if r["src"] == "解析失败":
                print("  ! 没解析到「通过 / 失败」汇总行，按失败处理。")
            lines = r["out"].rstrip().splitlines()
            for line in lines[-args.tail:]:
                print("  " + line)
        print("\n排查提示：")
        print("  · 端口被占用（8897/8899…）：先关掉面板与上一轮残留进程再跑")
        print("  · 缺包 / 模块：确认用的是 ..\\apienv\\Scripts\\python.exe")
        print("  · 只跑某几套：--only update,security")

    if not args.no_log:
        try:
            logdir = os.path.join(HERE, "logs")
            os.makedirs(logdir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            body = ["解释器：" + sys.executable, "目录：" + HERE,
                    "仓库根：%s" % (root or "未找到"), ""]
            for name, out in logs:
                body += ["", "=" * 70, "== " + name, "=" * 70, out.rstrip()]
            body += ["", "=" * 70,
                     "套数 %d 断言 %d 通过 %d 失败套数 %d 用时 %.1fs"
                     % (len(results), total_items, total_pass, len(bad), dur)]
            text = "\n".join(body) + "\n"
            for fn in ("e2e_run-%s.log" % stamp, "e2e_run-last.log"):
                with open(os.path.join(logdir, fn), "w", encoding="utf-8") as f:
                    f.write(text)
            print("\n日志：%s" % os.path.join(logdir, "e2e_run-last.log"))
        except Exception as e:  # noqa: BLE001
            print("\n[警告] 日志没写成：%r" % (e,))

    if bad:
        return 1
    if skipped and args.strict:
        print("\n--strict：有 %d 套被跳过，按失败退出。" % len(skipped))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
