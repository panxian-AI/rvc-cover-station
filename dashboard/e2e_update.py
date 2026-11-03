# -*- coding: utf-8 -*-
"""版本检测 / 在线更新回归测试。

不联网、不动真实安装目录：
    · 版本号解析与比较、VERSION 文件读取
    · 检测：有更新 / 已最新 / 网络失败 / 内存缓存 / 强制刷新
    · 更新：目录映射、数据与模型跳过、zip 越界路径拦截、备份、VERSION 写入
    · HTTP：/api/update/check 与 /api/update/apply（桩掉网络）
    · 控制台页面：版本卡片、弹窗、按钮都在

    python e2e_update.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8897"
os.environ["RVC_NO_AUTOPUMP"] = "1"

TMP = tempfile.mkdtemp(prefix="rvc_upd_")
os.environ["RVC_SETTINGS_PATH"] = os.path.join(TMP, "advanced_settings.json")
os.environ["RVC_GUEST_DB"] = os.path.join(TMP, "guest_db.json")

import panel      # noqa: E402
import updater    # noqa: E402

BASE = "http://127.0.0.1:8897"
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jget(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=25) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def jpost(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError as e:
        return 0, {"msg": str(e)}


def mk_zip(path, root="rvc-cover-station-1.0.6"):
    """造一个「更新包」：含代码、个人数据、模型、越界路径。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(root + "/dashboard/panel.py", "# new panel\n")
        z.writestr(root + "/dashboard/newmod.py", "# brand new\n")
        z.writestr(root + "/dashboard/guest_db.json", '{"evil": 1}')     # 必须跳过
        z.writestr(root + "/dashboard/advanced_settings.json", "{}")     # 必须跳过
        z.writestr(root + "/dashboard/__pycache__/x.pyc", "junk")        # 必须跳过
        z.writestr(root + "/RVCSVC-API/app_rvc.py", "# new api\n")
        z.writestr(root + "/RVCSVC-API/temp/keep.txt", "should skip")    # 必须跳过
        z.writestr(root + "/RVC-WebUI/webui.py", "# new webui\n")
        z.writestr(root + "/RVC-WebUI/assets/weights/m.pth", "model")    # 必须跳过
        z.writestr(root + "/ncmapi/api.js", "// new ncm\n")
        z.writestr(root + "/ncmapi/../../evil.py", "# escape")           # 越界，必须拦
        z.writestr(root + "/README.md", "# readme\n")                    # 不在映射里，忽略


def main():
    real = {
        "fetch": updater._fetch_release, "download": updater._download,
        "version_file": updater.VERSION_FILE, "backup": updater.BACKUP_DIR,
        "dir_map": updater.DIR_MAP, "cache_file": updater.CACHE_FILE,
    }
    sandbox = os.path.join(TMP, "install")
    for d in ("dashboard", "RVCSVC-API", "ncmapi", "RVC-WebUI", "setup"):
        os.makedirs(os.path.join(sandbox, d), exist_ok=True)
    updater.VERSION_FILE = os.path.join(TMP, "VERSION")
    updater.BACKUP_DIR = os.path.join(TMP, "backup")
    updater.CACHE_FILE = os.path.join(TMP, "update_cache.json")
    updater.DIR_MAP = [
        ("dashboard", os.path.join(sandbox, "dashboard")),
        ("RVCSVC-API", os.path.join(sandbox, "RVCSVC-API")),
        ("ncmapi", os.path.join(sandbox, "ncmapi")),
        ("setup", os.path.join(sandbox, "setup")),
        ("RVC-WebUI", os.path.join(sandbox, "RVC-WebUI")),
    ]
    # 沙箱里放两个「现有文件」：更新后应被替换并留备份；数据文件应保持原样
    old_panel = os.path.join(sandbox, "dashboard", "panel.py")
    with open(old_panel, "w", encoding="utf-8") as f:
        f.write("# old panel\n")
    db = os.path.join(sandbox, "dashboard", "guest_db.json")
    with open(db, "w", encoding="utf-8") as f:
        f.write('{"real": 1}')

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8897), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    try:
        print("\n== 1. 版本号解析 ==")
        chk("_tuple 认识 v 前缀", updater._tuple("v1.2.3") == (1, 2, 3), updater._tuple("v1.2.3"))
        chk("_tuple 补零", updater._tuple("1.2") == (1, 2, 0), updater._tuple("1.2"))
        chk("1.0.6 比 1.0.5 新", updater.is_newer("1.0.6", "1.0.5"))
        chk("1.0.5 不比 1.0.5 新", not updater.is_newer("1.0.5", "1.0.5"))
        chk("1.0.10 比 1.0.9 新（不是字符串比较）", updater.is_newer("1.0.10", "1.0.9"))
        chk("1.1 比 1.0.9 新", updater.is_newer("1.1", "1.0.9"))

        with open(updater.VERSION_FILE, "w", encoding="utf-8") as f:
            f.write("1.0.5\n")
        chk("读 VERSION 文件", updater.local_version() == "1.0.5", updater.local_version())
        with open(updater.VERSION_FILE, "w", encoding="utf-8") as f:
            f.write("乱写的版本\n")
        chk("VERSION 内容非法时回落", updater.local_version() == updater.FALLBACK_VERSION,
            updater.local_version())
        os.remove(updater.VERSION_FILE)
        chk("VERSION 缺失时回落", updater.local_version() == updater.FALLBACK_VERSION,
            updater.local_version())
        with open(updater.VERSION_FILE, "w", encoding="utf-8") as f:
            f.write("1.0.5\n")

        print("\n== 2. 检测：有新版本 / 已最新 / 缓存 ==")
        calls = []

        def fake_fetch():
            calls.append(1)
            return {"tag": "v1.0.6", "version": "1.0.6", "name": "RVC 翻唱站 v1.0.6",
                    "notes": "- 修个 bug\n- 加个功能", "url": "https://example.com/rel",
                    "published_at": "2026-11-03T10:00:00Z",
                    "asset": "", "asset_name": ""}

        updater._fetch_release = fake_fetch
        updater._cache = {"at": 0.0, "data": None}
        if os.path.isfile(updater.CACHE_FILE):
            os.remove(updater.CACHE_FILE)
        d1 = updater.check()
        chk("检测到新版本", d1.get("ok") and d1.get("has_update") and d1.get("latest") == "1.0.6",
            (d1.get("latest"), d1.get("has_update")))
        chk("带上更新说明与发布页", "修个 bug" in (d1.get("notes") or "")
            and d1.get("url") == "https://example.com/rel", d1.get("notes"))
        chk("第二次走内存缓存", updater.check().get("cached") and len(calls) == 1, len(calls))
        chk("force 会真的再查一次", (updater.check(force=True), len(calls))[1] == 2, len(calls))
        chk("磁盘缓存已落", os.path.isfile(updater.CACHE_FILE))

        # 已是最新
        def same_fetch():
            return {"tag": "v1.0.5", "version": "1.0.5", "name": "v1.0.5",
                    "notes": "", "url": "u", "published_at": "", "asset": "", "asset_name": ""}

        updater._fetch_release = same_fetch
        updater._cache = {"at": 0.0, "data": None}
        d2 = updater.check(force=True)
        chk("同版本不算有更新", d2.get("ok") and not d2.get("has_update"), d2.get("has_update"))

        # 网络失败
        def boom():
            raise TimeoutError("The read operation timed out")

        updater._fetch_release = boom
        updater._cache = {"at": 0.0, "data": None}
        if os.path.isfile(updater.CACHE_FILE):
            os.remove(updater.CACHE_FILE)
        d3 = updater.check(force=True)
        chk("网络失败不崩、给中文原因", (not d3.get("ok"))
            and "超时" in (d3.get("error") or ""), d3.get("error"))

        print("\n== 3. 更新：映射 / 跳过 / 越界 / 备份 ==")
        zpath = os.path.join(TMP, "pkg.zip")
        mk_zip(zpath)
        updater._fetch_release = fake_fetch
        updater._cache = {"at": 0.0, "data": None}
        updater._download = lambda tag: (open(zpath, "rb").read(), "https://stub/pkg.zip")
        res = updater.apply()
        chk("更新执行成功", res.get("ok"), res.get("msg"))
        chk("版本号已写到 1.0.6", updater.local_version() == "1.0.6", updater.local_version())
        with open(old_panel, encoding="utf-8") as f:
            chk("代码文件被替换", f.read().strip() == "# new panel")
        chk("新文件也进来了", os.path.isfile(os.path.join(sandbox, "dashboard", "newmod.py")))
        chk("API 目录跟着更新", os.path.isfile(os.path.join(sandbox, "RVCSVC-API", "app_rvc.py")))
        chk("WebUI 目录跟着更新", os.path.isfile(os.path.join(sandbox, "RVC-WebUI", "webui.py")))
        with open(db, encoding="utf-8") as f:
            chk("个人数据没被覆盖", f.read() == '{"real": 1}')
        chk("模型文件没被覆盖", not os.path.isfile(
            os.path.join(sandbox, "RVC-WebUI", "assets", "weights", "m.pth")))
        chk("temp 目录被跳过", not os.path.isfile(
            os.path.join(sandbox, "RVCSVC-API", "temp", "keep.txt")))
        chk("zip 越界路径被拦", not os.path.isfile(os.path.join(sandbox, "evil.py")))
        chk("映射外的文件不落地", not os.path.isfile(os.path.join(sandbox, "README.md")))
        chk("旧文件有备份", res.get("backed_up", 0) >= 1 and os.path.isfile(
            os.path.join(res.get("backup") or "", "dashboard", "panel.py")), res.get("backup"))
        chk("提示要重启", "重启" in (res.get("msg") or ""), res.get("msg"))

        print("\n== 4. 已是最新时拒绝更新 ==")
        updater._fetch_release = same_fetch
        updater._cache = {"at": 0.0, "data": None}
        # 让磁盘缓存也别捣乱
        if os.path.isfile(updater.CACHE_FILE):
            os.remove(updater.CACHE_FILE)
        res2 = updater.apply()
        chk("没有新版本就不动手", (not res2.get("ok")) and "最新" in (res2.get("msg") or ""),
            res2.get("msg"))

        print("\n== 5. 更新失败要说清楚 ==")
        with open(updater.VERSION_FILE, "w", encoding="utf-8") as f:
            f.write("1.0.5\n")      # 本地退回旧版，才会真的有更新可下
        def bad_dl(tag):
            raise RuntimeError("下载更新包失败：连接 GitHub 超时（网络或代理问题），稍后再试")

        updater._fetch_release = fake_fetch
        updater._cache = {"at": 0.0, "data": None}
        updater._download = bad_dl
        res3 = updater.apply()
        chk("下载失败有中文提示", (not res3.get("ok")) and "下载" in (res3.get("msg") or ""),
            res3.get("msg"))

        print("\n== 6. HTTP 接口 ==")
        updater._cache = {"at": 0.0, "data": None}
        updater._fetch_release = fake_fetch
        updater._download = lambda tag: (open(zpath, "rb").read(), "https://stub/pkg.zip")
        with open(updater.VERSION_FILE, "w", encoding="utf-8") as f:
            f.write("1.0.5\n")
        s1, r1 = jget("/api/update/check?force=1")
        chk("GET /api/update/check 正常", s1 == 200 and r1.get("ok")
            and r1.get("has_update") is True, (s1, r1.get("latest")))
        s2, r2 = jpost("/api/update/apply")
        chk("POST /api/update/apply 正常", s2 == 200 and r2.get("ok"), r2.get("msg"))
        chk("接口把版本写成 1.0.6", updater.local_version() == "1.0.6", updater.local_version())
        updater._fetch_release = same_fetch
        updater._cache = {"at": 0.0, "data": None}
        s3, r3 = jget("/api/update/check?force=1")
        chk("更新后已是最新", s3 == 200 and r3.get("has_update") is False, r3.get("latest"))
        s4, r4 = jpost("/api/update/apply")
        chk("再点更新给出提示", s4 == 400 and not r4.get("ok"), r4.get("msg"))

        print("\n== 7. 控制台页面元素 ==")
        with open(os.path.join(HERE, "console_page.html"), encoding="utf-8") as f:
            html = f.read()
        for token, label in [("verState", "版本状态标签"), ("检测更新", "手动检测按钮"),
                             ("立即更新", "更新按钮"), ("updModal", "更新弹窗"),
                             ("updCheck", "自动检测函数"), ("updApply", "更新执行函数"),
                             ("/api/update/check", "检测接口地址")]:
            chk("页面含" + label, token in html)
        chk("打开控制台会自动检测", "updCheck(false)" in html)
    finally:
        updater._fetch_release = real["fetch"]
        updater._download = real["download"]
        updater.VERSION_FILE = real["version_file"]
        updater.BACKUP_DIR = real["backup"]
        updater.DIR_MAP = real["dir_map"]
        updater.CACHE_FILE = real["cache_file"]
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
        print("\n=== %d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项：" + "；".join(FAIL))


if __name__ == "__main__":
    main()
