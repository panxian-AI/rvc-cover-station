# -*- coding: utf-8 -*-
"""隐私 / 抗滥用回归：这一轮加固的几处，逐条验证。

覆盖：
  1. 「我的申请」不再按 IP 找 —— 同一出口 IP 下的另一个人看不到你的单
  2. 访客 id 找回（?gid=）只认 16 位十六进制，且能看能下自己的
  3. 后端返回的路径必须落在允许的临时目录里，否则拒绝归档（防任意文件复制/删除）
  4. 并发连接上限：超过上限的连接被 503 挡掉，不会把线程耗光
  5. 安全日志同 IP 5 秒只记一条
  6. 没有 Pillow 时验证码拒绝出图（不退回明文 SVG）
  7. 访客计数写入被节流（不会每来一个人就整份 JSON 重写一次）

用 8893 起临时实例（不碰 8899），跑完还原 guest_db.json。
    python e2e_privacy.py
"""
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RVC_PANEL_PORT"] = "8893"
os.environ["RVC_NO_AUTOPUMP"] = "1"

import panel    # noqa: E402
import guest    # noqa: E402
import cover    # noqa: E402
import captcha  # noqa: E402

BASE = "http://127.0.0.1:8893"
DB = guest.DB_PATH
BAK = DB + ".priv_bak"
PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def req(path, method="GET", body=None, headers=None, timeout=20):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        return 0, {}, b""


def jget(path, **kw):
    code, _h, raw = req(path, **kw)
    try:
        return code, json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return code, {}


def make_req(gid, ip, title="测试歌曲", sid="910000001", acapella=False):
    """直接写库造一条申请（绕开验证码，只测查询/下载权限）。"""
    with guest._lock:
        r = {"id": guest.secrets.token_hex(6), "gid": gid, "ip": ip,
             "country": "", "ua": "测试", "type": "cover", "state": "pending",
             "created": time.time(), "decided": None, "job": None, "file": None,
             "error": None, "note": "", "song_id": sid, "title": title,
             "artist": "测试歌手", "model": "test.pth", "acapella": acapella,
             "text": ""}
        guest._db["requests"].insert(0, r)
        guest._save()
    return r


def main():
    if os.path.isfile(DB):
        shutil.copy2(DB, BAK)

    srv = panel.PanelServer(("127.0.0.1", 8893), panel.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    gid_a = "aaaaaaaaaaaaaaaa"
    gid_b = "bbbbbbbbbbbbbbbb"
    ip = "203.0.113.7"          # 两人共用同一个出口 IP
    made = []
    try:
        # ---------- 1. 不按 IP 串号 ----------
        print("\n== 1. 「我的申请」不再按 IP 兜底 ==")
        ra = make_req(gid_a, ip, title="A 的私密歌单")
        made.append(ra["id"])
        c, d = jget("/api/guest/my", headers={"Cookie": "rvc_guest=" + gid_b})
        titles = [x["title"] for x in d.get("requests", [])]
        chk("同 IP 的另一个人看不到我的申请", "A 的私密歌单" not in titles, titles[:3])
        chk("也不会被标成「已找回」", d.get("recovered") is False, d.get("recovered"))

        c, d2 = jget("/api/guest/my", headers={"Cookie": "rvc_guest=" + gid_a})
        chk("本人用 Cookie 能看到", any(x["title"] == "A 的私密歌单" for x in d2.get("requests", [])))

        # ---------- 2. gid 找回 ----------
        print("\n== 2. Cookie 丢了用 ?gid= 找回 ==")
        c, d3 = jget("/api/guest/my?gid=" + gid_a,
                     headers={"Cookie": "rvc_guest=" + gid_b})
        chk("凭 gid 找回自己的申请", any(x["title"] == "A 的私密歌单" for x in d3.get("requests", [])))
        chk("标记 recovered=True", d3.get("recovered") is True, d3.get("recovered"))
        chk("响应带回 gid 供前端记录", d3.get("gid") == gid_a, d3.get("gid"))

        for bad in ("../../etc/passwd", "abcdef", "AAAAAAAAAAAAAAAA", "a" * 32, "%27"):
            c, db = jget("/api/guest/my?gid=" + urllib.request.quote(bad),
                         headers={"Cookie": "rvc_guest=" + gid_b})
            titles = [x["title"] for x in db.get("requests", [])]
            chk("非法 gid %r 被忽略" % bad[:12], "A 的私密歌单" not in titles)

        # ---------- 3. 下载权限 ----------
        print("\n== 3. 下载：本人 / 找回者 / 陌生人 ==")
        real = next((i for i in cover._load_index()
                     if str(i.get("song_id") or "").isdigit() and cover._alive_file(i.get("name"))), None)
        if real:
            rd = make_req(gid_a, ip, title="带成品的单")
            made.append(rd["id"])
            with guest._lock:
                rd.update(state="done", file=os.path.basename(real["name"]), size_text="")
                guest._save()
            fname = os.path.basename(real["name"])
            q = "?rid=%s" % rd["id"]
            c1, _h, b1 = req("/api/guest/file" + q, headers={"Cookie": "rvc_guest=" + gid_b})
            chk("陌生人拿 rid 直接下 → 拒绝", c1 == 404, c1)
            c2, _h, b2 = req("/api/guest/file" + q, headers={"Cookie": "rvc_guest=" + gid_a})
            chk("本人能下", c2 == 200 and len(b2) > 1024, (c2, len(b2)))
            c3, _h, b3 = req("/api/guest/file" + q + "&gid=" + gid_a,
                             headers={"Cookie": "rvc_guest=" + gid_b})
            chk("凭 gid 找回后也能下", c3 == 200 and len(b3) > 1024, (c3, len(b3)))
            c4, _h, _b4 = req("/api/guest/file" + q + "&gid=" + gid_b,
                              headers={"Cookie": "rvc_guest=" + gid_b})
            chk("拿别人的 gid 下不来", c4 == 404, c4)
        else:
            chk("成品库里有样本可测下载", False, "index.json 里没有可用 mp3")

        # ---------- 4. 归档路径白名单 ----------
        print("\n== 4. 后端返回的路径必须可信 ==")
        chk("成品库自身不在白名单（不该拿它当源）",
            cover.trusted_src(os.path.join(cover.COVERS_DIR,
                                           os.path.basename(real["name"]))) is False)
        chk("C:\\Windows 下的文件被拒",
            cover.trusted_src(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                           "System32", "notepad.exe")) is False)
        chk("非 .mp3 被拒", cover.trusted_src(os.path.join(cover.BACKEND_TEMP, "x.txt")) is False)
        chk("桌面上的 mp3 被拒",
            cover.trusted_src(os.path.join(os.path.expanduser("~"), "Desktop", "x.mp3")) is False)
        probe = os.path.join(cover.tempfile.gettempdir(), "_rvc_probe_%d.mp3" % os.getpid())
        try:
            with open(probe, "wb") as f:
                f.write(b"ID3" + b"\x00" * 64)
            chk("系统临时目录里的真 mp3 放行", cover.trusted_src(probe) is True)
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
        try:
            cover._archive(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                        "System32", "notepad.exe"),
                           "标题", "歌手", "m.pth", 0, False, "1")
            chk("_archive 拒收不可信路径", False, "居然没抛异常")
        except RuntimeError:
            chk("_archive 拒收不可信路径", True)
        except Exception as e:
            chk("_archive 拒收不可信路径", False, type(e).__name__)

        # ---------- 5. 并发连接上限 ----------
        print("\n== 5. 并发连接上限 ==")
        socks = []
        try:
            for _ in range(panel.MAX_CONN + 8):
                s = socket.socket()
                s.settimeout(3)
                try:
                    s.connect(("127.0.0.1", 8893))
                    socks.append(s)
                except OSError:
                    s.close()
                    break
            # 槽位被上面这些「连上但不发请求」的连接占满，新连接应拿到 503
            got503 = 0
            for _ in range(6):
                s = socket.socket()
                s.settimeout(3)
                try:
                    s.connect(("127.0.0.1", 8893))
                    s.sendall(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    data = s.recv(200)
                    if b"503" in data:
                        got503 += 1
                except OSError:
                    pass
                finally:
                    s.close()
            chk("连接打满后新请求被 503 挡住", got503 >= 1, "503 次数 %d" % got503)
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass
        time.sleep(0.6)
        c, d = jget("/api/status")
        chk("槽位释放后恢复正常", c == 200 and d.get("ports"), c)

        # ---------- 6. 安全日志节流 ----------
        print("\n== 6. 安全日志节流 ==")
        before = os.path.getsize(panel.SEC_LOG) if os.path.isfile(panel.SEC_LOG) else 0
        for _ in range(10):
            panel.sec_log("测试节流 ip=198.51.100.9", ip="198.51.100.9")
        after = os.path.getsize(panel.SEC_LOG) if os.path.isfile(panel.SEC_LOG) else 0
        chk("同 IP 连打 10 次只写 1 条", after - before < 400, "增长 %d 字节" % (after - before))

        # ---------- 7. 验证码不降级成明文 ----------
        print("\n== 7. 没有 Pillow 时不出图 ==")
        real_draw = captcha._draw_png
        try:
            captcha._draw_png = lambda code: None
            c, d = jget("/api/guest/captcha", headers={"Cookie": "rvc_guest=" + gid_b})
            chk("出图失败 → 明确报错", d.get("ok") is False and "Pillow" in str(d.get("msg")), d.get("msg"))
            chk("不会退回 SVG / 不带答案",
                "img" not in d and "svg" not in json.dumps(d, ensure_ascii=False).lower())
        finally:
            captcha._draw_png = real_draw
        c, d = jget("/api/guest/captcha", headers={"Cookie": "rvc_guest=" + gid_b})
        chk("恢复后正常出 PNG", d.get("ok") is True and str(d.get("img", "")).startswith("data:image/png"))

        # ---------- 8. 访客计数写入节流 ----------
        print("\n== 8. 访客计数写盘节流 ==")
        n0 = 0
        orig_replace = os.replace
        hits = []

        def spy(src, dst):
            if dst == DB:
                hits.append(time.time())
            return orig_replace(src, dst)

        os.replace = spy
        try:
            for i in range(12):
                gid = ("c%015x" % i)[:16]
                guest.touch(gid, "198.51.100.%d" % (i + 20), "", "Mozilla/5.0")
        finally:
            os.replace = orig_replace
        n0 = len(hits)
        chk("12 个新访客不会写 12 次盘", n0 <= 3, "%d 次写盘" % n0)
        guest.flush()
        chk("flush() 能把挂起的改动补上", os.path.isfile(DB))

        # ---------- 9. 访客接口轻限流 ----------
        print("\n== 9. 访客接口轻限流（每分钟 120 次） ==")
        guest._api_hits.clear()
        codes = []
        for _ in range(guest.API_LIMIT + 8):
            c, _d = jget("/api/guest/exists?song_id=1",
                         headers={"Cookie": "rvc_guest=" + gid_b})
            codes.append(c)
        chk("前 120 次正常", codes.count(200) >= guest.API_LIMIT - 2, codes.count(200))
        chk("超出上限后 429", 429 in codes, "429 × %d" % codes.count(429))
        guest._api_hits.clear()
        c, _d = jget("/api/guest/exists?song_id=1", headers={"Cookie": "rvc_guest=" + gid_b})
        chk("清掉计数后立刻恢复", c == 200, c)

        # ---------- 10. 清理缓存的目标路径 ----------
        print("\n== 10. 清理缓存的目标路径校验 ==")
        chk("盘根被拒", cover._clearable("C:\\") is False)
        chk("Windows 目录被拒", cover._clearable(os.environ.get("WINDIR", r"C:\Windows")) is False)
        chk("Program Files 被拒", cover._clearable(r"C:\Program Files") is False)
        chk("用户主目录本身被拒", cover._clearable(os.path.expanduser("~")) is False)
        chk("不存在的目录被拒", cover._clearable(os.path.join(HERE, "_nope_xyz_")) is False)
        if os.path.isdir(cover.BACKEND_TEMP):
            chk("后端 temp 目录放行", cover._clearable(cover.BACKEND_TEMP) is True)
        if os.path.isdir(cover.gradio_temp_dir()):
            chk("gradio 缓存目录放行", cover._clearable(cover.gradio_temp_dir()) is True)

    finally:
        srv.shutdown()
        # 清掉本轮造的测试申请
        with guest._lock:
            guest._db["requests"] = [r for r in guest._db["requests"] if r["id"] not in made]
            guest._db["visits"] = [v for v in guest._db["visits"]
                                   if not str(v.get("ip", "")).startswith("198.51.100.")]
            guest._save()
        if os.path.isfile(BAK):
            shutil.copy2(BAK, DB)
            os.remove(BAK)
        print("\n=== %d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项：" + "；".join(FAIL))


if __name__ == "__main__":
    main()
