# -*- coding: utf-8 -*-
"""模块化 / 多页面 / 依赖锁 回归测试（v2.0.0）。

v2.0.0 干的三件事，各自都要有「以后别再退化」的守卫：

  A. 拆包：panel.py 只留装配，实现搬进 panelkit/；分层不许倒过来（core 不能
     import 上层），也不许出现循环导入
  B. 多页面：控制台拆成 概览/任务/访客/设置 四页 + assets/ 共用 css/js；
     每页引用的 id 必须在自己的 HTML 里，脚本不许跨页互相依赖
  C. 依赖锁：顶层 requirements.txt 全部 `==`，deps.py 能核对出漂移

跑法（必须在真实安装目录下）：
    apienv\\Scripts\\python.exe e2e_modules.py
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8971"
os.environ["RVC_NO_AUTOPUMP"] = "1"
os.environ.setdefault("RVC_GUEST_DB", os.path.join(HERE, "_e2e_modules_guest.json"))

import panel          # noqa: E402
import deps           # noqa: E402

PORT = 8971
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def rd(p):
    return open(os.path.join(HERE, p), encoding="utf-8").read().replace("\r\n", "\n")


def get(path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), headers={"Host": "127.0.0.1:%d" % PORT})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), dict(e.headers)
    except Exception as e:
        return -1, str(e), {}


# ---------------------------------------------------------------- A. 拆包

def test_layout():
    print("\n== A. 包结构 ==")
    src = rd("panel.py")
    n = len(src.split("\n"))
    chk("panel.py 只剩装配（≤ 300 行）", n <= 300, n)

    want = ["__init__.py", "core.py", "security.py", "sysinfo.py", "services.py",
            "pages.py", "server.py", "routes_get.py", "routes_post.py", "trayapp.py"]
    got = sorted(os.listdir(os.path.join(HERE, "panelkit")))
    chk("panelkit 十个文件都在", all(w in got for w in want), got)
    sizes = {w: len(rd(os.path.join("panelkit", w)).split("\n")) for w in want
             if w.endswith(".py") and w != "__init__.py"}
    big = {k: v for k, v in sizes.items() if v > 700}
    chk("没有哪个子模块回到 700 行以上", not big, big or sizes)

    # 分层：core 是最底层，不许 import 上面的东西
    core = rd("panelkit/core.py")
    chk("core 不 import 上层模块",
        not any(("from .%s import" % m) in core or ("from . import %s" % m) in core
                for m in ("security", "sysinfo", "services", "server", "routes_get",
                          "routes_post", "trayapp", "pages")), "core.py 干净")
    # 任何子模块都不许反过来 import panel（那才是真循环）
    bad = [w for w in got if w.endswith(".py")
           and re.search(r"^\s*(import panel\b|from panel import)", rd(os.path.join("panelkit", w)), re.M)]
    chk("子模块不反向 import panel", not bad, bad)

    # 干净进程里 import 一次：循环导入/漏导入都会在这里炸
    out = subprocess.run([sys.executable, "-c", "import panel; print(panel.PORT)"],
                         cwd=HERE, capture_output=True, text=True, timeout=120)
    chk("新进程 import panel 成功", out.returncode == 0 and "Traceback" not in out.stderr,
        (out.stdout or out.stderr)[-160:])

    # 门面：panel.X = v 要真的改到子模块（测试/工具常这么改端口表）
    old = panel.services.SERVICE_PORTS
    try:
        panel.SERVICE_PORTS = {"api": 12345}
        chk("panel.X = v 转发到持有它的子模块",
            panel.services.SERVICE_PORTS == {"api": 12345})
    finally:
        panel.SERVICE_PORTS = old
    chk("转发后读回来还是原表", panel.SERVICE_PORTS == panel.services.SERVICE_PORTS)


# ---------------------------------------------------------------- B. 多页面

PAGES = [
    ("/", "console_page.html", "page_overview.js"),
    ("/console/tasks", "console_tasks_page.html", "page_tasks.js"),
    ("/console/guest", "console_guest_page.html", "page_guest.js"),
    ("/console/settings", "console_settings_page.html", "page_settings.js"),
]


def test_pages():
    print("\n== B. 多页面 ==")
    srv = panel.PanelServer(("127.0.0.1", PORT), panel.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.6)
    try:
        for url, html_name, js_name in PAGES:
            st, body, hd = get(url)
            chk("%-18s 200 + HTML" % url, st == 200 and "text/html" in hd.get("Content-Type", ""), st)
            chk("  └ 引用了共用样式", 'href="/assets/console.css"' in body)
            chk("  └ 引用了自己的脚本", ('src="/assets/%s"' % js_name) in body)
            chk("  └ 没有内联大段 JS", body.count("<script>") == 0)

        st, css, hd = get("/assets/console.css")
        chk("/assets/console.css 200 text/css", st == 200 and "text/css" in hd.get("Content-Type", ""), st)
        chk("样式里有导航与卡片", ".nav" in css and ".card" in css and ".qrow" in css)

        for _u, _h, js in PAGES:
            st, code, hd = get("/assets/" + js)
            chk("/assets/%-18s 200 js" % js,
                st == 200 and "javascript" in hd.get("Content-Type", ""), st)
            chk("  └ 有刷新循环", "setInterval(refresh" in code)

        st, _b, hd = get("/assets/../panel.py")
        chk("静态资源不能越界", st == 404, st)
        st, _b, _h = get("/assets/nope.js")
        chk("不存在的资源 404", st == 404, st)
        st, _b, _h = get("/assets/deps.py")
        chk("非白名单后缀 404（.py 不给）", st == 404, st)

        st, body, _h = get("/api/deps")
        chk("/api/deps 返回核对结果", st == 200 and isinstance(json.loads(body).get("items"), list))

        # 每页的 id 与它自己脚本的引用对得上（漏一个就是运行时 TypeError）
        for _url, html_name, js_name in PAGES:
            h = rd(html_name)
            ids = set(re.findall(r'id="([^"]+)"', h))
            j = rd(os.path.join("assets", js_name))
            refs = set(re.findall(r"""\$\(['"]#([A-Za-z0-9_\-]+)['"]\)""", j))
            refs |= set(re.findall(r"""getElementById\(['"]([A-Za-z0-9_\-]+)['"]\)""", j))
            miss = sorted(refs - ids - {"dioSure"})     # dioSure 是预检时动态插进 #dioBox 的
            chk("%-24s 脚本引用的 id 都在页面上" % html_name, not miss, miss)

        # 导航要在每页都指到其它页（拆完页面最容易忘的就是这里）
        for _url, html_name, _js in PAGES:
            h = rd(html_name)
            links = re.findall(r'href="(/console/[a-z]+)"', h)
            chk("%-24s 导航齐全" % html_name, set(links) == {"/console/tasks", "/console/guest",
                                                              "/console/settings"}, sorted(set(links)))
    finally:
        srv.shutdown()
        time.sleep(0.3)


# ---------------------------------------------------------------- C. 依赖锁

def test_deps():
    print("\n== C. 依赖锁 ==")
    req = os.path.join(os.path.dirname(HERE), "requirements.txt")
    lines = [x.split("#")[0].strip() for x in open(req, encoding="utf-8").read().splitlines()]
    lines = [x for x in lines if x]
    chk("顶层 requirements.txt 存在且有内容", len(lines) >= 4, len(lines))
    loose = [x for x in lines if "==" not in x]
    chk("每一行都锁死版本（全部 ==）", not loose, loose)

    items = deps.parse(req)
    chk("parse 认得全部条目", len(items) == len(lines), (len(items), len(lines)))
    chk("解析出名字和版本", all(i["name"] and i["want"] for i in items),
        [i for i in items if not i["want"]])

    r = deps.check(force=True, path=req)
    chk("check 结构完整", all(k in r for k in ("ok", "items", "drift", "msg")), list(r))
    chk("check 能认出本机装了什么", any(i["have"] not in ("", "未安装") for i in r["items"]),
        r["msg"][:80])

    # 造一份「装的跟锁的不一样」的清单，必须报出来
    fake = os.path.join(HERE, "_e2e_req_bad.txt")
    try:
        open(fake, "w", encoding="utf-8").write("pillow==0.0.1\n# 注释行\n\nnot-a-real-pkg==9.9\n")
        r2 = deps.check(force=True, path=fake)
        chk("版本不符会被标出来", not r2["ok"] and any(x["status"] == "diff" for x in r2["drift"]),
            [x["status"] for x in r2["drift"]])
        chk("没装的包会被标出来", any(x["status"] == "missing" for x in r2["drift"]),
            r2["drift"])
    finally:
        if os.path.isfile(fake):
            os.remove(fake)

    cmd = deps.repair_cmd(req)
    chk("给出的修复命令能直接用", "pip install -r" in cmd and "requirements.txt" in cmd, cmd)


def test_release_plumbing():
    print("\n== D. 依赖锁真的生效（安装 / 更新 / 核对三段都得通） ==")
    import io as _io
    import zipfile as _zip

    import updater

    usrc = rd("updater.py")
    chk("更新器覆盖根级声明文件",
        {"requirements.txt", "install.json"} <= set(updater.ROOT_FILES), updater.ROOT_FILES)
    chk("apply 真的处理根级文件（不是只定义常量）",
        "rel in ROOT_FILES" in usrc and "os.path.join(PROJ, rel)" in usrc)

    # 更新包先验完整性：apply 是边解压边覆盖，坏包会留下半新半旧的代码
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello" * 200)
    good = buf.getvalue()
    chk("好包判为完整", updater._zip_broken(good) == "", updater._zip_broken(good)[:40])
    chk("截断的包会被判坏", bool(updater._zip_broken(good[: len(good) // 2])))
    chk("压根不是 zip 的也判坏", bool(updater._zip_broken(b"not a zip at all")))
    chk("下载流程里真的调了校验", "broken = _zip_broken(data)" in usrc)

    missing = os.path.join(HERE, "_nope_requirements.txt")
    r = deps.check(force=True, path=missing)
    chk("清单缺失时不再假装 ok", r.get("ok") is False and bool(r.get("missing_file")),
        r.get("msg"))
    chk("缺清单时不给「pip install」当修复建议",
        'd.get("missing_file")' in rd(os.path.join("panelkit", "routes_get.py")))

    # 导入必须走 guest 的统一写入口，否则并发写盘会把导入结果盖回旧数据
    gsrc = rd("guest.py")
    dsrc = rd("dataio.py")
    chk("guest 提供「整份替换」入口", "def replace_db(" in gsrc)
    chk("替换在锁内完成（换内存 + 落盘不被打断）",
        re.search(r"def replace_db\(.*?\n    with _lock:", gsrc, re.S) is not None)
    chk("dataio 写库不是直接覆盖文件", "_replace_guest(merged)" in dsrc)
    chk("回滚也走同一个入口", "_replace_guest(obj)" in dsrc)
    chk("合并取本机现状用内存快照",
        "def snapshot(" in gsrc and "_cur_db()" in dsrc)
    chk("写盘失败不吞（保留脏标记等下次重试）",
        "保留脏标记" in gsrc or "_dirty = True" in gsrc)


def main():
    print("RVC 模块化 / 多页面 / 依赖锁 回归测试")
    test_layout()
    test_pages()
    test_deps()
    test_release_plumbing()
    print("\n" + "=" * 52)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for x in FAIL:
            print("  - " + x)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
