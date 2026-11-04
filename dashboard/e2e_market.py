# -*- coding: utf-8 -*-
"""模型市场 / 多线路测速回归测试。

不联外网、不碰真实安装目录：
    · 线路测速排序（本地起两个端点，一个故意慢）
    · 搜索 / 详情解析（桩掉站点接口）、合集包补清单
    · 下载 → 大小核验 → SHA-256 核验 → torch 结构核验 → 安装（真下一遍，走本地 HTTP）
    · 各种非法入参（仓库名、路径穿越、错误扩展名、重复排队）
    · HTTP：/market 页面、/api/market/*、公网 403、CSRF 拦截
    · 更新器：多线路测速后按速度顺序尝试、失败降级

需要 torch（用 apienv 的 python 跑）：
    apienv\\Scripts\\python.exe e2e_market.py
"""
import hashlib
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8895"
os.environ["RVC_NO_AUTOPUMP"] = "1"

TMP = tempfile.mkdtemp(prefix="rvc_market_")
os.environ["RVC_MARKET_CACHE"] = os.path.join(TMP, "cache")
os.environ["RVC_MARKET_LINES_FILE"] = os.path.join(TMP, "market_lines.json")
os.environ["RVC_SETTINGS_PATH"] = os.path.join(TMP, "advanced_settings.json")
os.environ["RVC_GUEST_DB"] = os.path.join(TMP, "guest_db.json")
os.environ["RVC_PUBLIC_STATE"] = os.path.join(TMP, "public_access.json")

import market      # noqa: E402
import model_import as mi   # noqa: E402
import panel       # noqa: E402
import speed       # noqa: E402
import updater     # noqa: E402

BASE = "http://127.0.0.1:8895"
PASS, FAIL = [], []

# 安装目录指到临时文件夹（market 调的是 mi.install，读的是模块级常量）
mi.WEIGHTS_DIR = os.path.join(TMP, "weights")
mi.INDEX_DIR = os.path.join(TMP, "indices")
os.makedirs(mi.WEIGHTS_DIR, exist_ok=True)
os.makedirs(mi.INDEX_DIR, exist_ok=True)


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jget(path, hdrs=None):
    r = urllib.request.Request(BASE + path, headers=dict(hdrs or {}))
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


def jpost(path, body=None, hdrs=None):
    data = json.dumps(body or {}).encode()
    h = {"Content-Type": "application/json"}
    h.update(hdrs or {})
    r = urllib.request.Request(BASE + path, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
        except Exception:
            return e.code, {}
    except OSError:
        return 0, {}


# ---------------- 造文件 ----------------
def make_pth(path, sr="40k", f0=1, version="v2", info="30epoch"):
    """造一个能过 torch 结构识别的 RVC 声线 pth。"""
    import torch
    big = torch.zeros(300000)          # >1MB
    d = {"weight": {"enc_p.emb_phone.weight": torch.zeros(4, 4),
                    "enc_p.emb_phone.bias": torch.zeros(4),
                    "pad": big},
         "sr": sr, "f0": f0, "version": version, "info": info}
    torch.save(d, path)
    return path


def make_index(path):
    """faiss 索引只验魔数，头部 Iw 就够。"""
    with open(path, "wb") as f:
        f.write(b"IwFl" + b"\x00" * 4096)
    return path


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def size_of(p):
    return os.path.getsize(p)


# ---------------- 本地模型站（支持 Range / 慢速） ----------------
SERVED = {}          # 路径 -> 文件路径
SLOW_PREFIX = "/slow"


class Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        fp = SERVED.get(path)
        if not fp or not os.path.isfile(fp):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data_size = os.path.getsize(fp)
        start, end = 0, data_size - 1
        rng = self.headers.get("Range") or ""
        code = 200
        if rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-")[:2]
                start = int(a or 0)
                end = int(b) if b else data_size - 1
                end = min(end, data_size - 1)
                code = 206
            except Exception:
                start, end, code = 0, data_size - 1, 200
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, data_size))
        self.end_headers()
        slow = path.startswith(SLOW_PREFIX)
        with open(fp, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                if slow:
                    time.sleep(0.25)          # 故意慢：用于测速排序
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except OSError:
                    return
                left -= len(chunk)


SITE = ThreadingHTTPServer(("127.0.0.1", 0), Site)
SITE_PORT = SITE.server_address[1]
threading.Thread(target=SITE.serve_forever, daemon=True).start()
SITE_BASE = "http://127.0.0.1:%d" % SITE_PORT


def main():
    print("== 1. 多线路测速 ==")
    good = os.path.join(TMP, "probe.bin")
    with open(good, "wb") as f:
        f.write(os.urandom(1024 * 1024))
    SERVED["/fast/x.bin"] = good
    SERVED["/slow/x.bin"] = good
    res = speed.probe(SITE_BASE + "/fast/x.bin", nbytes=256 * 1024, timeout=8)
    chk("probe 能读到数据并算出速度", res.get("ok") and res.get("kbps", 0) > 0, res)
    res = speed.probe(SITE_BASE + "/nope.bin", nbytes=65536, timeout=5)
    chk("probe 404 不抛异常、标记失败", (not res.get("ok")) and res.get("code") == 404, res)
    ranked = speed.rank([
        {"label": "慢线", "url": SITE_BASE + "/slow/x.bin"},
        {"label": "快线", "url": SITE_BASE + "/fast/x.bin"},
        {"label": "断线", "url": SITE_BASE + "/nope.bin"},
    ], nbytes=256 * 1024, timeout=8)
    chk("rank 把快的排最前", ranked[0]["label"] == "快线", [r["label"] for r in ranked])
    chk("rank 保留失败线路（只排序不淘汰）", len(ranked) == 3 and
        ranked[-1]["label"] == "断线", [r["label"] for r in ranked])
    r2 = speed.rank([])
    chk("rank 空输入返回空表", r2 == [])

    print("== 2. 搜索 / 详情解析（桩掉站点接口）==")
    RAW_MODELS = [
        {"id": "u1/good-rvc", "downloads": 10, "likes": 2, "lastModified": "2026-01-02T00:00:00Z",
         "tags": ["rvc"], "siblings": [{"rfilename": "a.pth"}, {"rfilename": "a.index"}]},
        {"id": "u2/no-pth", "downloads": 99, "likes": 9, "siblings": [{"rfilename": "README.md"}]},
        {"id": "u3/llm-model", "downloads": 88, "likes": 8, "tags": ["text-generation"],
         "siblings": [{"rfilename": "big.pth"}]},
        {"id": "u4/gated-rvc", "gated": "manual", "downloads": 77,
         "siblings": [{"rfilename": "b.pth"}]},
        {"id": "u5/broken", "private": True, "siblings": [{"rfilename": "c.pth"}]},
        {"id": "u6/index-only", "siblings": [{"rfilename": "d.pth"}, {"rfilename": "d.index"}]},
    ]
    calls = []

    def fake_get_json(path, timeout=25, prefer=None):
        calls.append(path)
        if "filter=rvc" in path or "search=" in path:
            return RAW_MODELS, {"id": "local", "label": "测试线"}
        if "/api/models/" in path:
            return {"id": "u1/good-rvc", "downloads": 10, "likes": 2,
                    "lastModified": "2026-01-02T00:00:00Z",
                    "siblings": [
                        {"rfilename": "a.pth", "size": size_of(PTH_SRC),
                         "lfs": {"sha256": sha256_file(PTH_SRC), "size": size_of(PTH_SRC)}},
                        {"rfilename": "a.index", "size": size_of(IDX_SRC),
                         "lfs": {"sha256": sha256_file(IDX_SRC), "size": size_of(IDX_SRC)}},
                        {"rfilename": "README.md", "size": 12}]}, {"id": "local", "label": "测试线"}
        raise RuntimeError("unexpected path " + path)

    market._get_json = fake_get_json
    market._search_cache.clear()
    r = market.search("models", "", "downloads", 1, 20)
    chk("搜索过滤：没有 .pth 的仓库不出现", all(x["repo"] != "u2/no-pth" for x in r["items"]))
    chk("搜索过滤：纯文本生成模型的 .pth 不出现", all(x["repo"] != "u3/llm-model" for x in r["items"]))
    chk("搜索过滤：gated / private 跳过", all(x["repo"] not in ("u4/gated-rvc", "u5/broken")
                                        for x in r["items"]))
    chk("搜索保留带 rvc 标签或 .index 的仓库", any(x["repo"] == "u1/good-rvc" for x in r["items"]))
    chk("搜索返回线路名", r.get("line") == "测试线", r.get("line"))
    r_again = market.search("models", "", "downloads", 1, 20)
    chk("同一关键词走缓存（第二次不再请求）", len(calls) == 1, calls)
    chk("缓存命中结果一致", r_again["total"] == r["total"])

    d = market.detail("models", "u1/good-rvc")
    chk("详情解析出文件名与大小", d["ok"] and d["files"][0]["path"] == "a.pth", d.get("files"))
    chk("详情带 SHA-256", d["files"][0]["sha256"] == sha256_file(PTH_SRC))
    chk("详情统计 .pth/.index 数量", d["pth_n"] == 1 and d["index_n"] == 1)
    chk("详情按 pth → index → 其他 排序", [f["type"] for f in d["files"]] == ["pth", "index", "other"])
    chk("非法仓库名被拒", market.detail("models", "../etc/passwd")["ok"] is False)
    chk("带空格的仓库名被拒", market.detail("models", "a b/c")["ok"] is False)

    print("== 3. 入参校验 ==")
    chk("缺 pth 直接拒", market.start("models", "u1/good-rvc", "")["ok"] is False)
    chk("扩展名不是 .pth 拒", market.start("models", "u1/good-rvc", "a.index")["ok"] is False)
    chk("路径穿越拒", market.start("models", "u1/good-rvc", "../../x.pth")["ok"] is False)
    chk("反斜杠路径拒", market.start("models", "u1/good-rvc", "a\\b.pth")["ok"] is False)
    chk("绝对路径拒", market.start("models", "u1/good-rvc", "/etc/x.pth")["ok"] is False)
    chk("不认识的来源拒", market.start("nope", "u1/good-rvc", "a.pth")["ok"] is False)
    chk("非法仓库名拒", market.start("models", "bad repo/x", "a.pth")["ok"] is False)

    print("== 4. 下载 → 核验 → 安装（走本地 HTTP）==")
    os.environ["RVC_MARKET_LINES"] = "本地快线=%s,本地慢线=%s/slow" % (SITE_BASE, SITE_BASE)
    market.LINES = [{"id": "local", "label": "本地快线", "base": SITE_BASE},
                    {"id": "slow", "label": "本地慢线", "base": SITE_BASE + SLOW_PREFIX}]
    SERVED["/u1/good-rvc/resolve/main/a.pth"] = PTH_SRC
    SERVED["/slow/u1/good-rvc/resolve/main/a.pth"] = PTH_SRC
    SERVED["/u1/good-rvc/resolve/main/a.index"] = IDX_SRC
    SERVED["/slow/u1/good-rvc/resolve/main/a.index"] = IDX_SRC

    res = market.start("models", "u1/good-rvc", "a.pth", "a.index")
    chk("任务入队成功", res.get("ok") is True, res)
    chk("重复排队被拦", market.start("models", "u1/good-rvc", "a.pth", "a.index")["ok"] is False)
    jid = res["id"]
    job = wait_job(jid, 90)
    chk("任务完成", job.get("state") == "done", job)
    chk("进度到 100%", job.get("total") and job.get("done") >= job["total"],
        (job.get("done"), job.get("total")))
    chk("测速选中了快线", job.get("line") == "本地快线", job.get("line"))
    chk("声线装进 weights", os.path.isfile(os.path.join(mi.WEIGHTS_DIR, "a.pth")))
    chk("索引装进 indices", os.path.isfile(os.path.join(mi.INDEX_DIR, "a.index")))
    inst = job.get("installed") or {}
    chk("返回安装结果", inst.get("name") == "a.pth" and inst.get("index") == "a.index", inst)
    chk("装完提示重启后端", "重启" in (job.get("msg") or ""))
    chk("缓存里留有原文件", os.path.isfile(market._cache_path("models", "u1/good-rvc", "a.pth")))

    print("== 5. 坏文件拒收 ==")
    BAD = os.path.join(TMP, "bad.pth")
    with open(BAD, "wb") as f:
        f.write(b"NOTPK" + os.urandom(1024 * 1024))
    SERVED["/u1/good-rvc/resolve/main/bad.pth"] = BAD
    SERVED["/slow/u1/good-rvc/resolve/main/bad.pth"] = BAD
    before_detail = market._get_json

    def detail_with_bad(path, timeout=25, prefer=None):
        if "/api/models/" in path:
            return {"id": "u1/good-rvc", "siblings": [
                {"rfilename": "bad.pth", "size": size_of(BAD),
                 "lfs": {"sha256": sha256_file(BAD), "size": size_of(BAD)}}]}, \
                {"id": "local", "label": "测试线"}
        return before_detail(path, timeout, prefer)

    market._get_json = detail_with_bad
    r = market.start("models", "u1/good-rvc", "bad.pth", "")
    job = wait_job(r["id"], 60)
    chk("不是 PK 头的 pth 被拒", job.get("state") == "error" and "PyTorch" in (job.get("msg") or ""),
        job.get("msg"))
    chk("坏文件没有装进 weights", not os.path.isfile(os.path.join(mi.WEIGHTS_DIR, "bad.pth")))

    # SHA-256 不符：详情里写的哈希和真实文件不一致
    def detail_bad_sha(path, timeout=25, prefer=None):
        if "/api/models/" in path:
            return {"id": "u1/good-rvc", "siblings": [
                {"rfilename": "a.pth", "size": size_of(PTH_SRC),
                 "lfs": {"sha256": "0" * 64, "size": size_of(PTH_SRC)}}]}, \
                {"id": "local", "label": "测试线"}
        return before_detail(path, timeout, prefer)

    market._get_json = detail_bad_sha
    market._search_cache.clear()
    r = market.start("models", "u1/good-rvc", "a.pth", "")
    job = wait_job(r["id"], 60)
    chk("SHA-256 不符被拒", job.get("state") == "error" and "SHA-256" in (job.get("msg") or ""),
        job.get("msg"))

    def detail_404(path, timeout=25, prefer=None):
        raise RuntimeError("HTTP Error 404: Not Found")

    market._get_json = detail_404
    r = market.start("models", "u1/good-rvc", "a.pth", "")
    job = wait_job(r["id"], 60)
    chk("仓库没了给中文提示", job.get("state") == "error" and "没有这个仓库" in (job.get("msg") or ""),
        job.get("msg"))
    market._get_json = before_detail

    print("== 6. 本机已装 / 缓存 ==")
    inst = market.installed()
    chk("已装列表能列出刚装的声线", any(x["name"] == "a.pth" for x in inst["items"]), inst["items"])
    chk("已装列表标出索引状态", any(x["name"] == "a.pth" and x["has_index"] for x in inst["items"]))
    c = market.cache_info()
    chk("缓存统计有大小", c["ok"] and c["mb"] > 0, c)
    market.clear_finished()
    chk("清空完成后任务列表为空", market.jobs() == [])

    print("== 7. 自定义市场地址（线路）==")
    real_probe = market._probe
    market._probe = lambda base, timeout=8.0: {"ok": True, "ms": 12, "error": ""}
    # 前面几个小节用 RVC_MARKET_LINES 把线路指到本地站点，这里先摘掉，测完再挂回去
    saved_env = os.environ.pop("RVC_MARKET_LINES", None)
    market._clear_line_caches()
    base_ids = [x["id"] for x in market.all_lines()]
    chk("默认有两条内置线路", base_ids == ["mirror", "official"], base_ids)
    chk("缺协议的地址被拒", market.line_add("x", "hf-mirror.com")["ok"] is False)
    chk("ftp 协议被拒", market.line_add("x", "ftp://a.example.com")["ok"] is False)
    chk("带账号密码的地址被拒", market.line_add("x", "https://u:p@a.example.com")["ok"] is False)
    chk("地址太长被拒", market.line_add("x", "https://a.example.com/" + "x" * 300)["ok"] is False)
    r_add = market.line_add("我的镜像", "https://Mirror.Example.com/hf/")
    chk("合法地址能加进去", r_add["ok"] is True, r_add.get("msg"))
    chk("地址被规范化（去尾斜杠、域名小写）", r_add.get("base") == "https://mirror.example.com/hf",
        r_add.get("base"))
    chk("重复地址被拒", market.line_add("", "https://mirror.example.com/hf")["ok"] is False)
    chk("新线路参与选线", any(x["base"] == "https://mirror.example.com/hf" for x in market._lines()))
    cid = r_add["id"]
    chk("可以停用线路", market.line_toggle("mirror", False)["ok"] is True)
    chk("停用后不参与选线", all(x["id"] != "mirror" for x in market._lines()))
    chk("停用状态在 all_lines 里可见",
        any(x["id"] == "mirror" and not x["enabled"] for x in market.all_lines()))
    chk("再停用一条也行", market.line_toggle("official", False)["ok"] is True)
    chk("不能停用最后一条线路", market.line_toggle(cid, False)["ok"] is False)
    market.line_toggle("mirror", True)
    market.line_toggle("official", True)
    chk("重新启用后两条都在", len(market._lines()) == 3, [x["id"] for x in market._lines()])
    chk("内置线路不能删", market.line_remove("mirror")["ok"] is False)
    chk("删除自定义线路", market.line_remove(cid)["ok"] is True)
    chk("删完就没了", all(x["id"] != cid for x in market.all_lines()))

    cfg_path = os.environ["RVC_MARKET_LINES_FILE"]
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as fp:
        fp.write("{坏掉的 JSON")
    market._cfg_cache["mtime"] = -1.0
    market._clear_line_caches()
    chk("配置文件损坏时回落默认线路",
        [x["id"] for x in market._lines()] == ["mirror", "official"])

    os.environ["RVC_MARKET_LINES"] = "测试A=https://a.example.com,测试B=https://b.example.com"
    market._clear_line_caches()
    chk("环境变量优先于界面配置", [x["label"] for x in market._lines()] == ["测试A", "测试B"],
        [x["label"] for x in market._lines()])
    chk("环境变量生效时界面改动被拒", market.line_add("x", "https://c.example.com")["ok"] is False)
    st = market.lines_status(False)
    chk("线路状态标出 locked", st.get("locked") is True and len(st["lines"]) == 2, st.get("lines"))
    del os.environ["RVC_MARKET_LINES"]
    market._clear_line_caches()
    chk("去掉环境变量后恢复默认", [x["id"] for x in market._lines()] == ["mirror", "official"])

    market._probe = lambda base, timeout=8.0: {"ok": False, "ms": 5, "error": "连不上"}
    r_dead = market.line_add("坏线路", "https://dead.example.com")
    chk("连不上的地址也能先加着（提示不可达）",
        r_dead["ok"] is True and "连不上" in r_dead.get("msg", ""), r_dead.get("msg"))
    market.line_remove(r_dead["id"])
    market._probe = real_probe
    if saved_env:
        os.environ["RVC_MARKET_LINES"] = saved_env
    market._clear_line_caches()

    print("== 8. HTTP 接口 ==")
    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8895), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    code, body = jget("/market")
    chk("GET /market 200", code == 200, code)
    chk("市场页面含标题与关键词", "模型市场" in body and "/api/market/search" in body)
    chk("市场页面有自定义地址入口", "自定义市场地址" in body and "line_add" in body)
    chk("市场页面有「更新声线」按钮", "更新声线" in body and "restartApi" in body)

    # 页面热更新：改完 HTML 刷新浏览器就生效，不用重启面板
    tp = os.path.join(HERE, "_tmp_hot_page.html")
    try:
        open(tp, "w", encoding="utf-8").write("A")
        chk("页面首次读取", panel._read_page("_tmp_hot_page.html", "") == "A")
        open(tp, "w", encoding="utf-8").write("B")
        os.utime(tp, (time.time() + 5, time.time() + 5))   # 确保 mtime 变化
        chk("文件改动后按 mtime 重读（热更新）", panel._read_page("_tmp_hot_page.html", "") == "B")
        os.remove(tp)
        chk("文件缺失时回落到传入的兜底内容",
            panel._read_page("_tmp_hot_page.html", "FALLBACK") == "FALLBACK")
    finally:
        if os.path.isfile(tp):
            os.remove(tp)
        panel._PAGE_CACHE.pop("_tmp_hot_page.html", None)

    # 更新声线走的是 /api/action 的 restart；这里桩掉真实启停，只验路由与返回语义
    real_stop, real_start = panel.stop, panel.start
    panel.stop = lambda t: (True, "桩：已停 %s" % t)
    panel.start = lambda t: (True, "桩：已启 %s" % t)
    try:
        code, obj = jpost("/api/action", {"do": "restart", "target": "api"})
    finally:
        panel.stop, panel.start = real_stop, real_start
    chk("POST /api/action restart 能重启 api",
        code == 200 and obj.get("ok") is True and "已启" in (obj.get("msg") or ""), obj)

    code, body = jget("/api/market/jobs")
    chk("GET /api/market/jobs 正常", code == 200 and json.loads(body)["ok"] is True)

    code, body = jget("/api/market/installed")
    obj = json.loads(body)
    chk("GET /api/market/installed 带已装数量", code == 200 and obj["n"] >= 1, obj.get("n"))

    code, body = jget("/api/market/search?kind=models&q=&per=6")
    obj = json.loads(body)
    chk("GET /api/market/search 返回结果", code == 200 and obj["ok"], obj.get("total"))

    code, obj = jpost("/api/market/install", {"kind": "models", "repo": "u1/good-rvc", "pth": ""})
    chk("POST install 缺 pth 被拒", obj.get("ok") is False, obj)

    code, obj = jpost("/api/market/cancel", {"id": "none"})
    chk("POST cancel 不存在的任务给提示", obj.get("ok") is False)

    code, body = jget("/market", {"X-Forwarded-For": "1.2.3.4"})
    chk("带转发头的公网请求拿不到市场页", code == 403, code)
    code, obj = jpost("/api/market/install", {"kind": "models", "repo": "u1/good-rvc", "pth": "a.pth"},
                      {"Origin": "http://evil.example"})
    chk("跨站 POST 被 CSRF 拦", code == 403, code)

    market._probe = lambda base, timeout=8.0: {"ok": True, "ms": 30, "error": ""}
    saved_env2 = os.environ.pop("RVC_MARKET_LINES", None)
    market._clear_line_caches()
    code, body = jget("/api/market/lines")
    obj = json.loads(body)
    chk("GET /api/market/lines 带 builtin/enabled", code == 200 and obj.get("ok") is True
        and all("builtin" in x and "enabled" in x for x in obj["lines"]), obj.get("lines"))
    chk("线路状态带可自定义上限", obj.get("custom_max", 0) >= 1)
    code, obj = jpost("/api/market/line_add", {"label": "HTTP线", "base": "https://http.example.com/"})
    chk("POST line_add 能加线路", obj.get("ok") is True, obj.get("msg"))
    hid = obj.get("id")
    code, body = jget("/api/market/lines")
    chk("页面能查到新线路", "HTTP线" in body)
    code, obj = jpost("/api/market/line_toggle", {"id": "mirror", "on": False})
    chk("POST line_toggle 能停用", obj.get("ok") is True, obj.get("msg"))
    code, body = jget("/api/market/lines?fresh=1")
    chk("停用的线路标 skipped",
        any(x["id"] == "mirror" and x.get("skipped") for x in json.loads(body)["lines"]))
    code, obj = jpost("/api/market/line_remove", {"id": hid})
    chk("POST line_remove 能删掉", obj.get("ok") is True, obj.get("msg"))
    code, obj = jpost("/api/market/line_toggle", {"id": "mirror", "on": True})
    chk("恢复内置线路", obj.get("ok") is True)
    market._probe = real_probe
    if saved_env2:
        os.environ["RVC_MARKET_LINES"] = saved_env2
    market._clear_line_caches()

    print("== 9. 更新器多线路测速 ==")
    seen = []

    def zip_bytes():
        """真 zip：更新包现在会先验 CRC，拿假数据（只有 PK 头）会被判坏包。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr("VERSION", "9.9.9")
            z.writestr("padding.txt", "x" * 4096)   # 过 1024 字节的「像样 zip」门槛
        return buf.getvalue()

    def fake_get(url, timeout=12):
        seen.append(url)
        if "ghproxy.net" in url:
            raise OSError("boom")
        return zip_bytes()

    real_get, real_rank = updater._get, updater.speed.rank
    updater._get = fake_get

    def fake_rank(cands, **kw):
        out = []
        for c in cands:
            ok = "ghproxy.net" not in c["url"]
            out.append(dict(c, ok=ok, kbps=100 if "ghfast" in c["url"] else 1, ms=5))
        out.sort(key=lambda x: (not x["ok"], -x["kbps"]))
        return out

    updater.speed.rank = fake_rank
    data, used = updater._download("v9.9.9")
    chk("按测速顺序优先用最快的线路", "ghfast.top" in used, used)
    chk("下载成功返回 PK 开头的数据", data[:2] == b"PK")
    chk("记录了线路测速明细", len(updater._last_download.get("lines") or []) == 8,
        len(updater._last_download.get("lines") or []))
    chk("最快的线路不在第一位时也会被选中", seen and "ghfast" in seen[0], seen[:2])

    # 最快那一路失败 → 自动降级到下一路
    seen.clear()

    def fake_get2(url, timeout=12):
        seen.append(url)
        if "ghfast.top" in url:
            raise OSError("boom")
        return zip_bytes()

    updater._get = fake_get2
    data, used = updater._download("v9.9.9")
    chk("最快线路失败后自动换线", used and "ghfast" not in used, used)
    chk("失败后仍能拿到数据", data[:2] == b"PK")

    # 包被截断（有 PK 头但内容不全）→ 判坏包并换线，绝不能拿去覆盖代码
    seen.clear()

    def fake_get_bad(url, timeout=12):
        seen.append(url)
        if "ghfast.top" in url:
            return zip_bytes()[:40]
        return zip_bytes()

    updater._get = fake_get_bad
    data, used = updater._download("v9.9.9")
    chk("坏包不会被当成好包用", "ghfast" not in used, used)
    chk("坏包之后换了别的线路", len(seen) >= 2 and data[:2] == b"PK")

    # 全失败 → 中文报错
    def fake_get3(url, timeout=12):
        raise OSError("timed out")

    updater._get = fake_get3
    try:
        updater._download("v9.9.9")
        chk("所有线路都失败时抛中文错误", False)
    except RuntimeError as e:
        chk("所有线路都失败时抛中文错误", "下载更新包失败" in str(e), str(e))

    updater._get, updater.speed.rank = real_get, real_rank

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：", f)
        return 1
    return 0


def wait_job(jid, timeout=90):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        for j in market.jobs():
            if j["id"] == jid:
                last = j
                if j["state"] in ("done", "error", "cancelled"):
                    return j
        time.sleep(0.3)
    return last


PTH_SRC = make_pth(os.path.join(TMP, "src_a.pth"))
IDX_SRC = make_index(os.path.join(TMP, "src_a.index"))

if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            SITE.shutdown()
        except Exception:
            pass
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
