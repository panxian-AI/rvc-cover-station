# -*- coding: utf-8 -*-
"""模型导入回归测试：识别、合适性判定、配对、安装、净化、权限。

用 8896 起一个临时面板实例（不碰 8899），安装目录全部指到临时文件夹。
需要 torch（用 apienv 的 python 跑）：
    apienv\\Scripts\\python.exe e2e_model_import.py
"""
import json
import re
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8896"
os.environ["RVC_NO_AUTOPUMP"] = "1"

import panel   # noqa: E402
import model_import as mi   # noqa: E402

BASE = "http://127.0.0.1:8896"
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def jpost(path, body=None, hdrs=None):
    data = json.dumps(body or {}).encode()
    h = {"Content-Type": "application/json"}
    h.update(hdrs or {})
    r = urllib.request.Request(BASE + path, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
    except OSError:
        return 0, {}


def jget(path, hdrs=None):
    h = dict(hdrs or {})
    r = urllib.request.Request(BASE + path, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


def make_pth(path, sr="40k", f0=1, version="v2", info="30epoch",
             rvc=True, weight=True):
    """造一个（或故意造坏的）RVC 声线 pth。"""
    import torch
    big = torch.zeros(300000)          # >1MB，过大小闸
    w = {"enc_p.emb_phone.weight": torch.zeros(4, 4),
         "enc_p.emb_phone.bias": torch.zeros(4)} if rvc else {"a": big}
    w["pad"] = big
    if not weight:
        d = {"state_dict": w, "sr": sr}
    else:
        d = {"weight": w, "sr": sr, "f0": f0, "version": version,
             "info": info, "epoch": 30}
    torch.save(d, path)


def make_index(path, magic=b"IwFl"):
    with open(path, "wb") as f:
        f.write(magic + b"\x00" * 4096)


def main():
    tmp = tempfile.mkdtemp(prefix="rvc_mi_")
    wdir = os.path.join(tmp, "weights")
    idir = os.path.join(tmp, "indices")
    mi.WEIGHTS_DIR, mi.INDEX_DIR = wdir, idir     # 安装目标指向临时目录

    srv = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 8896), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    try:
        print("\n== 1. pth 识别与合适性判定 ==")
        good = os.path.join(tmp, "feiyu.pth")
        make_pth(good)
        r = mi.inspect_pth(good)
        chk("正常声线被认出来", r.get("ok") is True, r.get("msg"))
        chk("采样率归一化 40k", r.get("sr") == "40k", r.get("sr"))
        chk("版本 v2", r.get("version") == "v2")
        chk("f0=True", r.get("f0") is True)
        chk("轮数从 info 提取", r.get("epoch") == 30, r.get("epoch"))

        applio = os.path.join(tmp, "applio.pth")
        make_pth(applio, sr=40000, f0=True, version="v2", info="")
        r2 = mi.inspect_pth(applio)
        chk("Applio 风格（sr=40000 整数）也认", r2.get("ok") and r2.get("sr") == "40k",
            (r2.get("sr"), r2.get("msg")))

        missing_v = os.path.join(tmp, "old.pth")
        make_pth(missing_v, version=None)
        r3 = mi.inspect_pth(missing_v)
        chk("老模型缺版本号 → 仍可用但给提醒", r3.get("ok") and
            any("版本" in w for w in r3.get("warns", [])), r3.get("warns"))

        notrvc = os.path.join(tmp, "other.pth")
        make_pth(notrvc, rvc=False)
        chk("权重没有 enc_p 特征层 → 拒", mi.inspect_pth(notrvc).get("ok") is False)

        noweight = os.path.join(tmp, "ckpt.pth")
        make_pth(noweight, weight=False)
        chk("普通训练检查点（缺 weight）→ 拒", mi.inspect_pth(noweight).get("ok") is False)

        garbage = os.path.join(tmp, "garbage.pth")
        open(garbage, "wb").write(b"x" * 2048 * 1024)
        chk("乱码 pth → 拒", mi.inspect_pth(garbage).get("ok") is False)

        small = os.path.join(tmp, "small.pth")
        open(small, "wb").write(b"x" * 100)
        chk("太小的 pth → 拒", mi.inspect_pth(small).get("ok") is False)

        txt = os.path.join(tmp, "a.txt")
        open(txt, "w").write("hi")
        chk("txt 也能被 inspect（按 pth 处理给出明确失败原因）",
            mi.inspect_pth(txt).get("ok") is False)

        chk("不存在的路径 → 拒而不是抛异常",
            mi.inspect_pth(os.path.join(tmp, "nope.pth")).get("ok") is False)

        print("\n== 2. index 识别 ==")
        ix = os.path.join(tmp, "feiyu_added_v2.index")
        make_index(ix)
        r = mi.inspect_index(ix)
        chk("faiss 索引认出来", r.get("ok") is True)
        chk("基名提取 feiyu_added_v2 → feiyu", r.get("base") == "feiyu", r.get("base"))
        chk("Applio 长名基名提取",
            mi.index_base("deepseek_added_IVF1641_Flat_nprobe_1_deepseek_v2.index")
            == "deepseek")
        chk("经典名基名提取", mi.index_base("luotianyi_added.index") == "luotianyi")
        badix = os.path.join(tmp, "bad.index")
        make_index(badix, magic=b"NOPE")
        chk("非 faiss 魔数 → 拒", mi.inspect_index(badix).get("ok") is False)

        print("\n== 3. check 批量识别与配对 ==")
        c = mi.check([good, ix])
        chk("配对成功", c.get("pairs") == [{"pth": "feiyu.pth",
                                            "index": "feiyu_added_v2.index"}],
            c.get("pairs"))
        c = mi.check([good, os.path.join(tmp, "feiyu_v1.index")] if False else [good])
        chk("没选索引时不乱配", c["items"][0].get("match_index") is None)
        c = mi.check([good, ix, txt])
        chk("不认识的扩展名被标出来",
            any(x.get("type") == "?" and not x.get("ok") for x in c["items"]))
        chk("check 拒绝非列表", mi.check("abc").get("ok") is False)
        chk("check 拒绝空列表", mi.check([]).get("ok") is False)
        chk("check 拒绝超量", mi.check(["x"] * 13).get("ok") is False)

        print("\n== 4. install 安装 ==")
        r = mi.install(good, ix)
        chk("安装成功", r.get("ok") is True, r.get("msg"))
        chk("pth 落在 weights 目录", os.path.isfile(os.path.join(wdir, "feiyu.pth")))
        chk("index 落在 indices 目录", os.path.isfile(os.path.join(idir, "feiyu_added_v2.index")))
        chk("落盘大小一致", os.path.getsize(os.path.join(wdir, "feiyu.pth"))
            == os.path.getsize(good))

        r = mi.install(good, ix)
        chk("同名默认拒绝并提示覆盖", r.get("ok") is False and r.get("exists") is True,
            r.get("msg"))
        r = mi.install(good, ix, replace=True)
        chk("显式覆盖成功", r.get("ok") is True)

        # 净化是单元行为：非法字符出现在「从 JSON 传来的路径」里，
        # 落盘名一定走 _clean_name，这里直接测净化函数本身
        chk("非法文件名字符被净化",
            not re.search(r'[\\/:*?"<>|]', mi._clean_name('bad<>|:"name.pth')),
            mi._clean_name('bad<>|:"name.pth'))
        chk("路径穿越只剩最后一段",
            mi._clean_name(r"C:\x\..\..\evil.pth") == "evil.pth",
            mi._clean_name(r"C:\x\..\..\evil.pth"))
        spaced = os.path.join(tmp, "我的 声线 v2.pth")
        shutil.copy2(good, spaced)
        r = mi.install(spaced, replace=True)
        chk("中文/空格文件名原样保留", r.get("ok") and r.get("pth") == "我的 声线 v2.pth",
            r.get("pth"))

        trav = os.path.join(tmp, "t.pth")
        make_pth(trav)
        r = mi.install(trav, index=None, replace=True)
        chk("正常路径安装不受净化影响", r.get("ok") is True)

        r = mi.install(notrvc)
        chk("不合格的 pth 装不进去", r.get("ok") is False, r.get("msg"))
        r = mi.install("")
        chk("空路径 → 拒", r.get("ok") is False)
        r = mi.install(good, index=badix)
        chk("坏索引连累整次安装 → 拒", r.get("ok") is False, r.get("msg"))

        # 索引名与模型名对不上 → 自动改成 <模型名>_added_v2.index
        other = os.path.join(tmp, "zhuruoxian.index")
        make_index(other)
        r = mi.install(applio, other, replace=True)
        chk("索引名对不上时按模型名重命名",
            r.get("ok") and r.get("index") == "applio_added_v2.index", r.get("index"))

        print("\n== 5. HTTP 路由（含权限） ==")
        s, d = jpost("/api/admin/import_check", {"paths": [good, ix]})
        chk("HTTP check 通", s == 200 and d.get("ok") and
            d.get("pairs") == [{"pth": "feiyu.pth", "index": "feiyu_added_v2.index"}])
        s, d = jpost("/api/admin/import_check", {"paths": "abc"})
        chk("HTTP 参数不合法被拒", s == 200 and d.get("ok") is False)
        s, d = jpost("/api/admin/import_install",
                     {"pth": garbage, "replace": True})
        chk("HTTP 安装坏 pth 被拒", s == 200 and d.get("ok") is False)
        # pick 会弹真窗口，桩掉弹窗只验证「路由 → pick_files → 返回」链路
        real_pick = mi.pick_files
        mi.pick_files = lambda: {"ok": True, "paths": [good, ix]}
        try:
            s, d = jpost("/api/admin/import_pick", {})
        finally:
            mi.pick_files = real_pick
        chk("pick 路由走通（桩掉弹窗）",
            s == 200 and d.get("ok") and d.get("paths") == [good, ix], (s, d))
        # 远程 + 带转发头 → local_only 必须拒
        s, _ = jpost("/api/admin/import_check", {"paths": []},
                     {"CF-Connecting-IP": "1.2.3.4"})
        chk("带 CF 头的管理请求被 403", s == 403, s)
        s, _ = jpost("/api/admin/import_check", {"paths": []},
                     {"X-Forwarded-For": "1.2.3.4"})
        chk("带 XFF 头的管理请求被 403", s == 403, s)
        s, html = jget("/")
        chk("控制台含模型导入卡片", "模型导入" in html and "importPick" in html)
        chk("控制台含导入结果渲染与安装函数",
            "impRender" in html and "importInstall" in html)
    finally:
        srv.shutdown()
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 结果: %d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
