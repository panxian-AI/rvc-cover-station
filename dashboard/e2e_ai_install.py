# -*- coding: utf-8 -*-
"""AI 安装入口回归测试（给 Agent 用的一页纸 + 无人值守脚本）。

测的是「让 AI 自己装」这条链路，不联网、不装真东西：
    · AI-INSTALL.md / install.json 的必备内容与版本一致性
    · ai_install.ps1 的编码（UTF-8 BOM，否则 PS5.1 中文全乱）、语法、参数
    · 一键安装.bat 的无人值守钩子（不弹窗、不问显卡、失败退出码非 0）
    · 用假安装器跑通 ai_install.ps1：环境变量传递、产物校验、成功/失败 JSON

脚本放在仓库里（`dashboard/e2e_ai_install.py`）。找不到仓库根目录就跳过，
所以装到用户的运行目录里也不会红。

    apienv\\Scripts\\python.exe e2e_ai_install.py
    RVC_REPO_DIR=D:\\rvc-cover-station apienv\\Scripts\\python.exe e2e_ai_install.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def find_root():
    env = (os.environ.get("RVC_REPO_DIR") or "").strip()
    if env and os.path.isfile(os.path.join(env, "一键安装.bat")):
        return os.path.abspath(env)
    up = os.path.dirname(HERE)
    if os.path.isfile(os.path.join(up, "一键安装.bat")):
        return up
    return ""


TMP = tempfile.mkdtemp(prefix="rvc_ai_")


def write_bat(path, body):
    """GBK + CRLF，和真安装器一致。"""
    io.open(path, "w", encoding="gbk", newline="\r\n").write(body)


FAKE_OK = """@echo off
setlocal
if "%RVC_DRYRUN%"=="1" (
  echo [dry-run] %RVC_DEFDIR%
  exit /b 0
)
> "%RVC_DEFDIR%\\_env.txt" echo DIR=%RVC_DEFDIR%
>> "%RVC_DEFDIR%\\_env.txt" echo GPU=%RVC_GPU%
>> "%RVC_DEFDIR%\\_env.txt" echo NOSTART=%RVC_NO_START%
>> "%RVC_DEFDIR%\\_env.txt" echo TAG=%RVC_TAG%
>> "%RVC_DEFDIR%\\_env.txt" echo UNATT=%RVC_UNATTENDED%
>> "%RVC_DEFDIR%\\_env.txt" echo PREFIX=%RVC_GH_PREFIX%
mkdir "%RVC_DEFDIR%\\RVC-Data\\RVC-WebUI\\.venv\\Scripts" 2>nul
mkdir "%RVC_DEFDIR%\\RVC-Data\\RVCSVC\\apienv\\Scripts" 2>nul
mkdir "%RVC_DEFDIR%\\RVC-Data\\RVCSVC\\dashboard" 2>nul
echo x> "%RVC_DEFDIR%\\RVC-Data\\RVC-WebUI\\webui.py"
echo x> "%RVC_DEFDIR%\\RVC-Data\\RVC-WebUI\\.venv\\Scripts\\python.exe"
echo x> "%RVC_DEFDIR%\\RVC-Data\\RVCSVC\\apienv\\Scripts\\python.exe"
echo x> "%RVC_DEFDIR%\\RVC-Data\\RVCSVC\\dashboard\\panel.py"
echo 9.9.9> "%RVC_DEFDIR%\\RVC-Data\\RVCSVC\\VERSION"
echo x> "%RVC_DEFDIR%\\启动RVC.bat"
echo AI_RESULT ok dir=%RVC_DEFDIR%
endlocal
exit /b 0
"""

FAKE_PARTIAL = """@echo off
setlocal
mkdir "%RVC_DEFDIR%\\RVC-Data\\RVC-WebUI" 2>nul
echo x> "%RVC_DEFDIR%\\RVC-Data\\RVC-WebUI\\webui.py"
endlocal
exit /b 0
"""

FAKE_FAIL = """@echo off
setlocal
> "%RVC_DEFDIR%\\_install_failed.txt" echo Stage=deps
>> "%RVC_DEFDIR%\\_install_failed.txt" echo Time=now
echo AI_RESULT fail stage=deps
endlocal
exit /b 1
"""


def run_ps1(ps1, args, timeout=120):
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1] + args
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", "ignore")
    err = (r.stderr or b"").decode("utf-8", "ignore")
    obj = None
    for line in reversed([x.strip() for x in out.splitlines() if x.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                break
            except ValueError:
                continue
    return r.returncode, obj, out, err


def main():
    root = find_root()
    if not root:
        print("跳过：没找到仓库根目录（一键安装.bat）。装好后的运行目录里就是这样，不算失败。")
        return 0
    print("仓库根目录：", root)

    md = os.path.join(root, "AI-INSTALL.md")
    js = os.path.join(root, "install.json")
    ps1 = os.path.join(root, "install", "ai_install.ps1")
    bat = os.path.join(root, "一键安装.bat")

    print("== 1. 给 AI 的一页纸 ==")
    chk("AI-INSTALL.md 存在", os.path.isfile(md))
    text = io.open(md, encoding="utf-8").read() if os.path.isfile(md) else ""
    chk("写清理了「不需要读其它文件」", "不需要读仓库里的其它文件" in text)
    chk("给出入口 URL", "install/ai_install.ps1" in text)
    chk("说明成功判定（ok）", '"ok":true' in text.replace(" ", ""))
    chk("说明先空跑（-DryRun）", "-DryRun" in text)
    chk("有失败对照表（Stage）", "Stage=deps" in text and "Stage=python" in text)
    chk("把用户要做的事写清楚了（启动RVC.bat）", "启动RVC.bat" in text)

    print("== 2. install.json 机器清单 ==")
    chk("install.json 存在", os.path.isfile(js))
    data = {}
    if os.path.isfile(js):
        try:
            data = json.load(io.open(js, encoding="utf-8"))
        except ValueError as e:
            data = {}
            print("     JSON 解析失败：", e)
    chk("是合法 JSON", bool(data))
    ver = (io.open(os.path.join(root, "VERSION"), encoding="utf-8").read().strip()
           if os.path.isfile(os.path.join(root, "VERSION")) else "")
    chk("版本号和 VERSION 一致", data.get("version") == ver, (data.get("version"), ver))
    ai = data.get("ai_install") or {}
    chk("声明了入口地址", "raw.githubusercontent.com" in (ai.get("entry_url") or ""))
    chk("声明了参数表", set((ai.get("args") or {})) >= {"Dir", "Gpu", "Tag", "DryRun"})
    chk("声明了成功条件", "ok" in (ai.get("result") or {}).get("success_when", ""))
    chk("声明了校验点", len(data.get("verify") or []) >= 4)
    chk("声明了免管理员", (data.get("requirements") or {}).get("admin") is False)
    chk("入口文件确实在仓库里",
        os.path.isfile(os.path.join(root, *str(ai.get("entry_path", "x")).split("/"))))

    print("== 3. ai_install.ps1 ==")
    chk("脚本存在", os.path.isfile(ps1))
    raw = open(ps1, "rb").read() if os.path.isfile(ps1) else b""
    chk("带 UTF-8 BOM（PS5.1 中文不乱码）", raw[:3] == b"\xef\xbb\xbf", raw[:3])
    chk("是 CRLF 换行", b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"))
    src = raw.decode("utf-8-sig", "ignore")
    for a in ("$Dir", "$Gpu", "$Tag", "$Installer", "$Start", "$DryRun"):
        chk("声明参数 %s" % a, a in src)
    chk("无人值守时不让 bat 自己启动面板", "RVC_NO_START" in src)
    chk("把探到的线路传给 bat（省一次测速失败）", "RVC_GH_PREFIX" in src)
    chk("会把 LF 版安装器修成 CRLF", "function Repair-Bat" in src)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                        "'" + ps1.replace("'", "''") + "',[ref]$null,[ref]$e);"
                        "if($e){$e[0].Message}else{'OK'}"],
                       capture_output=True, timeout=60)
    parse_msg = (r.stdout or b"").decode("utf-8", "ignore").strip()
    chk("PowerShell 语法解析通过", parse_msg == "OK", parse_msg[:120])

    print("== 4. 一键安装.bat 的无人值守钩子 ==")
    rawb = open(bat, "rb").read() if os.path.isfile(bat) else b""
    try:
        btxt = rawb.decode("gbk")
        dec_ok = True
    except UnicodeDecodeError:
        btxt, dec_ok = "", False
    chk("是 GBK 编码（cmd 下中文不乱码）", dec_ok)
    chk("是 CRLF 换行", rawb.count(b"\n") == rawb.count(b"\r\n"))
    chk("传了 RVC_DEFDIR 就不弹文件夹选择框", "if defined RVC_DEFDIR goto :dir_given" in btxt)
    chk("无人值守跳过线路确认", 'if /i "%RVC_UNATTENDED%"=="1" goto :gh_done' in btxt)
    chk("无人值守跳过显卡询问", 'if /i "%RVC_GPU%"=="gpu" goto :mode_done' in btxt)
    chk("无人值守不自动启动面板", 'if /i "%RVC_NO_START%"=="1" goto :bye' in btxt)
    chk("支持 dry-run 钩子", 'if /i "%RVC_DRYRUN%"=="1" (' in btxt)
    chk("测速全挂时用外部线路兜底", 'if defined RVC_GH_PREFIX (' in btxt)
    chk("成功时写 _install_ok.txt", "_install_ok.txt" in btxt)
    chk("失败时写 _install_failed.txt 并退出码 1",
        "_install_failed.txt" in btxt and "exit /b 1" in btxt)
    chk("安装位置会归一化（挡盘根）", 'for %%p in ("%INSTALL%") do set "INSTALL=%%~fp"' in btxt)

    print("== 5. 用假安装器跑编排 ==")
    ok_bat = os.path.join(TMP, "ok.bat")
    partial_bat = os.path.join(TMP, "partial.bat")
    fail_bat = os.path.join(TMP, "fail.bat")
    write_bat(ok_bat, FAKE_OK)
    write_bat(partial_bat, FAKE_PARTIAL)
    write_bat(fail_bat, FAKE_FAIL)

    d1 = os.path.join(TMP, "case_ok")
    code, obj, out, err = run_ps1(ps1, ["-Dir", d1, "-Gpu", "cpu", "-Installer", ok_bat])
    chk("假安装器跑完退出码 0", code == 0, (code, err[:200]))
    chk("输出了 JSON 且 ok=true", bool(obj) and obj.get("ok") is True, obj)
    chk("JSON 里的版本号来自 VERSION 文件", (obj or {}).get("version") == "9.9.9", (obj or {}).get("version"))
    chk("没有 missing", (obj or {}).get("missing") == [], (obj or {}).get("missing"))
    envtxt = ""
    p = os.path.join(d1, "_env.txt")
    if os.path.isfile(p):
        envtxt = io.open(p, encoding="gbk", errors="ignore").read()
    chk("把安装目录传给了 bat", ("DIR=" + d1) in envtxt, envtxt.splitlines()[:2])
    chk("把显卡模式传给了 bat", "GPU=cpu" in envtxt)
    chk("无人值守默认不启动面板", "NOSTART=1" in envtxt)
    chk("开了无人值守开关", "UNATT=1" in envtxt)

    d2 = os.path.join(TMP, "case_start")
    code, obj, out, err = run_ps1(ps1, ["-Dir", d2, "-Gpu", "cpu", "-Start", "-Installer", ok_bat])
    envtxt = io.open(os.path.join(d2, "_env.txt"), encoding="gbk", errors="ignore").read()
    chk("-Start 时告诉 bat 可以启动", "NOSTART=0" in envtxt, envtxt.splitlines()[:4])
    chk("-Start 那次也成功", code == 0 and (obj or {}).get("ok") is True)

    d3 = os.path.join(TMP, "case_tag")
    code, obj, out, err = run_ps1(ps1, ["-Dir", d3, "-Gpu", "cpu", "-Tag", "v1.1.0", "-Installer", ok_bat])
    envtxt = io.open(os.path.join(d3, "_env.txt"), encoding="gbk", errors="ignore").read()
    chk("-Tag 会锁版本传给 bat", "TAG=v1.1.0" in envtxt, envtxt.splitlines()[:5])

    d4 = os.path.join(TMP, "case_missing")
    code, obj, out, err = run_ps1(ps1, ["-Dir", d4, "-Gpu", "cpu", "-Installer", partial_bat])
    chk("产物不全时 ok=false", code == 1 and (obj or {}).get("ok") is False, obj)
    chk("缺什么写在 missing 里", len((obj or {}).get("missing") or []) >= 2, (obj or {}).get("missing"))
    chk("缺产物时 error 有中文说明", "缺少文件" in ((obj or {}).get("error") or ""), (obj or {}).get("error"))

    d5 = os.path.join(TMP, "case_fail")
    code, obj, out, err = run_ps1(ps1, ["-Dir", d5, "-Gpu", "cpu", "-Installer", fail_bat])
    chk("安装器失败时 ok=false 且退出码 1", code == 1 and (obj or {}).get("ok") is False, obj)
    chk("失败原因从 _install_failed.txt 读出来", "Stage=deps" in ((obj or {}).get("error") or ""),
        (obj or {}).get("error"))

    code, obj, out, err = run_ps1(ps1, ["-Dir", "C:\\", "-Gpu", "cpu", "-Installer", ok_bat])
    chk("盘根目录被拒", code == 1 and (obj or {}).get("ok") is False, obj)
    chk("拒绝原因说得清", "盘根" in ((obj or {}).get("error") or ""), (obj or {}).get("error"))

    code, obj, out, err = run_ps1(ps1, ["-Dir", os.path.join(TMP, "x"), "-DryRun", "-Installer", ok_bat])
    chk("dry-run 不创建产物目录", not os.path.isfile(os.path.join(TMP, "x", "_env.txt")))
    chk("dry-run 也返回 JSON", bool(obj) and obj.get("ok") is True, obj)

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：", f)
        return 1
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
