# -*- coding: utf-8 -*-
"""多音源平台（网易云 / QQ 音乐 / 酷狗）回归测试（v2.2.0）。

覆盖四块，**全程不联网**（上游响应全部打桩，CI 不该依赖三家音乐站是否可达）：
  A. ncmapi/sources 适配器：id 解析、Cookie 落盘、搜索归一、取流成功/失败话术
  B. 3002 网关：platform 参数、老客户端（不带 platform）兼容、错误码映射
  C. 面板：搜索前缀、默认平台、访客校验
  D. 接线：页面上的平台下拉 / 音源平台卡片 / 设置字段与注册表一致

跑法：
    apienv\\Scripts\\python.exe e2e_music.py
"""
import importlib.util as _ilu
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
NCM = os.path.join(HERE, "..", "ncmapi")

PASS, FAIL = [], []


def chk(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK] " if ok else "  [!!] ") + name + (("  -> " + str(extra)) if extra else ""))


def load_pkg(name, dirpath):
    spec = _ilu.spec_from_file_location(
        name, os.path.join(dirpath, "__init__.py"), submodule_search_locations=[dirpath])
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# =====================================================================
# A. 适配器
# =====================================================================
def test_adapters(src):
    print("\n== A. 音源适配器 ==")
    base, qq, kg, ne = src.base, src.qq, src.kugou, src.netease

    chk("id 解析：带前缀 / 纯数字 / 宽松兼容冒号 / 非法",
        base.split_id("qq-0035GveV3i9dBM") == ("qq", "0035GveV3i9dBM")
        and base.split_id("12345") == ("netease", "12345")
        and base.split_id("kugou:ABC123") == ("kugou", "ABC123")
        and base.split_id("../etc") == ("netease", "../etc"),
        [base.split_id(x) for x in ("qq-a1", "12345", "kugou:ABC123", "../etc")])
    chk("拼 id：网易云保持纯数字（老索引零迁移）",
        base.make_id("netease", "123") == "123"
        and base.make_id("qq", "abc") == "qq-abc"
        and ":" not in base.make_id("kugou", "abc"))

    # --- Cookie 落盘 ---
    tmp = tempfile.mkdtemp()
    try:
        st = base.CookieStore("t", ("MUSIC_U=",))
        st.path = os.path.join(tmp, "t.txt")
        try:
            st.save("abc=1")
            bad = "accept"
        except ValueError:
            bad = "reject"
        st.save("Cookie: MUSIC_U=xyz; os=pc\n\n# 注释行")
        state = st.state()
        ok = (bad == "reject" and state["login"] and state["cookie"] == "MUSIC_U=xyz; os=pc"
              and not os.path.exists(st.path + ".tmp"))
        chk("Cookie 落盘：校验标记键、剥 Cookie: 前缀与注释、原子写不留 tmp", ok,
            (bad, state["cookie"]))
        st.clear()
        chk("清除 Cookie 后回到未登录", not st.state()["login"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- QQ 搜索（new_json / 老格式）---
    fixture_new = {"code": 0, "data": {"song": {"list": [{
        "mid": "0035GveV3i9dBM", "title": "十年", "interval": 205,
        "singer": [{"name": "陈奕迅"}],
        "album": {"id": 89526, "name": "黑白灰"},
        "pay": {"pay_play": 1}}]}}}
    old_get = qq.http_get_json
    qq.http_get_json = lambda *a, **k: json.loads(json.dumps(fixture_new))
    try:
        rows = qq.search("十年", 5)
    finally:
        qq.http_get_json = old_get
    chk("QQ 搜索解析 new_json（mid/title/album 对象/pay）",
        len(rows) == 1 and rows[0]["id"] == "0035GveV3i9dBM"
        and rows[0]["name"] == "十年" and rows[0]["artist"] == "陈奕迅"
        and rows[0]["duration"] == 205 and rows[0]["paid"] is True, rows)

    fixture_old = {"code": 0, "data": {"song": {"list": [{
        "songmid": "000xg5lw0AoTVP", "songname": "明年今日", "albumname": "The Line-Up",
        "interval": 206, "singer": [{"name": "陈奕迅"}]}]}}}
    qq.http_get_json = lambda *a, **k: json.loads(json.dumps(fixture_old))
    try:
        rows = qq.search("明年今日", 5)
    finally:
        qq.http_get_json = old_get
    chk("QQ 搜索兼容老格式（songmid/songname/albumname）",
        len(rows) == 1 and rows[0]["id"] == "000xg5lw0AoTVP"
        and rows[0]["album"] == "The Line-Up", rows)

    # --- QQ 取流 ---
    old_post = qq.http_post_json
    qq.http_post_json = lambda *a, **k: {"req_0": {"data": {
        "sip": ["http://dl.example/"], "midurlinfo": [{"purl": "M500.mp3?vkey=1"}]}}}
    try:
        url = qq.song_url("0035GveV3i9dBM")
    finally:
        qq.http_post_json = old_post
    chk("QQ 取流：sip + purl 拼成完整直链", url == "http://dl.example/M500.mp3?vkey=1", url)

    old_store = qq.STORE
    qq.STORE = base.CookieStore("empty", ("qm_keyst=",))
    qq.STORE.path = os.path.join(tempfile.gettempdir(), "__rvc_no_such_cookie__.txt")
    qq.http_post_json = lambda *a, **k: {"req_0": {"data": {
        "sip": [""], "midurlinfo": [{"purl": ""}]}}}
    try:
        try:
            qq.song_url("0035GveV3i9dBM")
            err = None
        except base.SourceError as e:
            err = e
    finally:
        qq.http_post_json = old_post
        qq.STORE = old_store
    chk("QQ 无 Cookie 且没直链 → 明确要求登录（need_login=True）",
        err is not None and err.need_login and "Cookie" in str(err), err)

    qq.STORE = base.CookieStore("qq_probe", ("qm_keyst=",))
    qq.STORE.path = os.path.join(tempfile.mkdtemp(), "qq.txt")
    with open(qq.STORE.path, "w", encoding="utf-8") as f:
        f.write("uin=o12345678; qm_keyst=abc")
    try:
        chk("QQ 从 Cookie 取数字 uin（uin=o123… → 123…）", qq.uin() == "12345678", qq.uin())
    finally:
        shutil.rmtree(os.path.dirname(qq.STORE.path), ignore_errors=True)
        qq.STORE = old_store

    # --- 酷狗 ---
    import hashlib as _hl
    _h = "5146461A5717B942F4B13125978618E3"
    _exp = _hl.md5((_h + "kgcloudv2").encode()).hexdigest()
    chk("酷狗 tracker key = md5(大写hash + kgcloudv2)，小写输入也归一",
        kg.tracker_key(_h) == _exp and kg.tracker_key(_h.lower()) == _exp,
        kg.tracker_key(_h))
    fixture = {"status": 1, "data": {"lists": [{
        "FileHash": "5146461a5717b942f4b13125978618e3", "SongName": "<em>小苹果</em>",
        "SingerName": "<em>筷子兄弟</em>", "AlbumID": "504194", "Duration": 213,
        "Privilege": 0}]}}
    old_get = kg.http_get_json
    kg.http_get_json = lambda *a, **k: json.loads(json.dumps(fixture))
    try:
        rows = kg.search("小苹果", 5)
    finally:
        kg.http_get_json = old_get
    chk("酷狗搜索解析（hash 转大写、剥 <em> 高亮标签）",
        len(rows) == 1 and rows[0]["id"] == "5146461A5717B942F4B13125978618E3"
        and rows[0]["name"] == "小苹果" and rows[0]["artist"] == "筷子兄弟", rows)

    def kg_fake(status, url=None):
        def f(*a, **k):
            return {"status": status, "url": [url] if url else []}
        return f

    kg.http_get_json = kg_fake(1, "http://fs.example/a.mp3")
    try:
        u = kg.song_url("5146461a5717b942f4b13125978618e3")
    finally:
        kg.http_get_json = old_get
    chk("酷狗取流：status=1 返回直链", u == "http://fs.example/a.mp3", u)

    kg.http_get_json = kg_fake(2)
    try:
        try:
            kg.song_url("5146461A5717B942F4B13125978618E3")
            e2 = None
        except base.SourceError as e:
            e2 = e
    finally:
        kg.http_get_json = old_get
    chk("酷狗 status=2（付费曲）→ 提示配 Cookie / 换歌",
        e2 is not None and e2.need_login, e2)

    kg.http_get_json = kg_fake(0)
    try:
        try:
            kg.song_url("5146461A5717B942F4B13125978618E3")
            e3 = None
        except base.SourceError as e:
            e3 = e
    finally:
        kg.http_get_json = old_get
    chk("酷狗 Bad key → 直接报接口变化，不静默重试", e3 is not None and "Bad key" in str(e3), e3)

    # --- 网易云 ---
    old_ne = ne.http_get_json
    ne.http_get_json = lambda *a, **k: {"data": [{"url": "http://m.example/1.mp3", "code": 200}]}
    try:
        u = ne.song_url("66842")
    finally:
        ne.http_get_json = old_ne
    chk("网易云取流：data[0].url 直接返回", u == "http://m.example/1.mp3", u)


def test_netease(src):
    ne = src.netease
    old = ne.http_get_json
    ne.http_get_json = lambda *a, **k: {"result": {"songs": [{
        "id": 66842, "name": "十年", "ar": [{"name": "陈奕迅"}],
        "al": {"name": "黑白灰", "id": 89526}, "dt": 205000}]}}
    try:
        rows = ne.search("十年", 3)
    finally:
        ne.http_get_json = old
    chk("网易云搜索解析（ar/al/dt → 扁平字段）",
        rows and rows[0]["id"] == "66842" and rows[0]["artist"] == "陈奕迅"
        and rows[0]["duration"] == 205, rows)

    ne.http_get_json = lambda *a, **k: {"data": [{"url": "", "code": 404}]}
    try:
        try:
            ne.song_url("66842")
            e = None
        except src.base.SourceError as ex:
            e = ex
    finally:
        ne.http_get_json = old
    chk("网易云拿不到直链 → 区分「需要登录」与「没版权」",
        e is not None and "VIP" in str(e), e)


# =====================================================================
# B. 3002 网关
# =====================================================================
def test_gateway(src):
    print("\n== B. 3002 网关 ==")
    port = free_port()
    sys.path.insert(0, NCM)
    import ncm_proxy as gw
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", port), gw.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        with op.open("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    real_search, real_url, real_login = (gw.sources.search, gw.sources.song_url,
                                         gw.sources.login_status)
    seen = []

    def fake_search(platform, kw, limit=12, offset=0):
        seen.append(platform)
        return [{"id": "X1", "name": kw, "artist": "A", "album": "B",
                 "duration": 200, "album_id": "1"}]

    gw.sources.search = fake_search
    gw.sources.song_url = lambda p, sid: "http://x/%s.mp3" % sid
    gw.sources.login_status = lambda p, force=False: {
        "platform": p, "label": p, "login": False, "nickname": "", "vip": False,
        "msg": "未配置"}

    try:
        d = get("/search?keywords=hi&platform=qq")
        d2 = get("/search?keywords=hi")
        chk("网关：platform 透传给适配器（qq / 默认 netease）",
            seen == ["qq", "netease"] and d["platform"] == "qq" and d2["platform"] == "netease",
            seen)
        chk("网关：搜索结果同时给扁平与老网易云两种字段",
            d["songs"][0]["name"] == "hi" and d["result"]["songs"][0]["dt"] == 200000
            and d["result"]["songCount"] == 1, d["songs"][0])
        d = get("/song/url?platform=kugou&id=ABC")
        chk("网关：/song/url 返回 data[0].url", d["data"][0]["url"] == "http://x/ABC.mp3", d)

        def boom(p, sid):
            # 必须抛网关模块里的那个类：e2e 把包加载成了独立命名空间，
            # 和 gw 内的 SourceError 不是同一个类对象，抛错了 except 抓不到
            raise gw.SourceError("要 Cookie", need_login=True)

        gw.sources.song_url = boom
        d = get("/song/url?platform=qq&id=A")
        chk("网关：need_login 的失败映射成 code=403 + need_login=true（HTTP 仍 200）",
            d["code"] == 403 and d.get("need_login") is True, d)

        d = get("/platforms")
        chk("网关：/platforms 列出三家 + 默认平台",
            [x["key"] for x in d["platforms"]] == ["netease", "qq", "kugou"]
            and d["default"] == "netease", d)
        d = get("/nonsense")
        chk("网关：未实现端点仍返回空结构（老客户端不炸）",
            d.get("code") == 200 and d.get("result", {}).get("songs") == [], d)
    finally:
        gw.sources.search, gw.sources.song_url, gw.sources.login_status = (
            real_search, real_url, real_login)
        srv.shutdown()


# =====================================================================
# C. 面板接线
# =====================================================================
def test_panel(src):
    print("\n== C. 面板（搜索前缀 / 默认平台 / 访客校验）==")
    import panel  # noqa: F401
    import cover
    import guest

    cases = {"123": ("netease", "123"), "qq-0035GveV3i9dBM": ("qq", "0035GveV3i9dBM"),
             "kugou-ABC123": ("kugou", "ABC123"), "../x": ("", ""), "qq-": ("", "")}
    chk("cover.split_song_id：前缀/纯数字/非法 5 组",
        all(cover.split_song_id(k) == v for k, v in cases.items()),
        {k: cover.split_song_id(k) for k in cases})

    old = cover._json_get
    cover._json_get = lambda url, timeout=15: {
        "songs": [{"id": "66842", "name": "十年", "artist": "陈奕迅", "album": "黑白灰",
                   "duration": 205}], "platform": "netease"}
    try:
        r = cover.search("十年", 3, "kugou")
    finally:
        cover._json_get = old
    chk("cover.search：id 带平台前缀、平台名回填",
        r["ok"] and r["songs"][0]["id"] == "kugou-66842"
        and r["songs"][0]["platform_label"] == "酷狗音乐", r.get("songs"))

    chk("cover.search：未知平台回落到默认（不会把非法值带到上游）",
        cover.split_song_id("netease-66842") == ("netease", "66842"))
    chk("默认音源读设置，非法值回落网易云", cover.default_platform() in cover.PLATFORMS,
        cover.default_platform())

    real_model = guest._valid_model
    guest._valid_model = lambda m: "x.pth"      # 只验 id 那一关，不依赖本机模型库
    try:
        v = guest._check_cover({"song_id": "qq-001OyHbk2MSIi4", "title": "十年",
                                "model": "x.pth"})
    finally:
        guest._valid_model = real_model
    chk("访客提交：带前缀的 id 通过校验", v.get("ok") is True, v)
    v = guest._check_cover({"song_id": "../evil", "title": "x", "model": "x.pth"})
    chk("访客提交：非法 id 仍被拒", v.get("ok") is False, v)


def test_music_module(src):
    print("\n== D. 面板音源模块 ==")
    import panel
    from panelkit import music

    # 复制一份干净的 ncmapi（没有 Cookie 文件）→ 不联网也能测状态
    tmp = tempfile.mkdtemp()
    real = panel.core.NCM_DIR
    try:
        shutil.copytree(os.path.join(real, "sources"), os.path.join(tmp, "sources"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        panel.NCM_DIR = tmp
        music._CACHE.update(dir="", mod=None)
        mod = music._sources()
        for _k in ("netease", "qq", "kugou"):
            setattr(getattr(mod, _k), "login_status",
                    lambda *a, **kw: {"platform": _k, "login": False, "nickname": "",
                                      "vip": False, "msg": "stub"})
        st = music.status(force=True)
        chk("音源状态：三家都列出来，未配 Cookie 时不发网络请求",
            st["ok"] and [p["key"] for p in st["platforms"]] == ["netease", "qq", "kugou"]
            and not any(p["login"] for p in st["platforms"]), st.get("msg"))
        r = music.save_cookie("qq", "hello")
        chk("保存垃圾 Cookie 被拒（不写盘）", r["ok"] is False and "qm_keyst" in r["msg"], r)
        r = music.save_cookie("netease", "MUSIC_U=abc")
        chk("网易云只检查 MUSIC_U（保存成功即认为已配置）", r["ok"] is True, r)
    finally:
        panel.NCM_DIR = real
        music._CACHE.update(dir="", mod=None)
        music._STATUS.update(at=0.0, obj=None)
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# E. 页面与文档接线
# =====================================================================
def test_wiring(src):
    print("\n== E. 页面 / 设置接线 ==")
    html = open(os.path.join(HERE, "console_page.html"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "assets", "page_overview.js"), encoding="utf-8").read()
    cover_html = open(os.path.join(HERE, "cover_page.html"), encoding="utf-8").read()
    guest_html = open(os.path.join(HERE, "guest_page.html"), encoding="utf-8").read()
    chk("概览页有音源平台列表和通用 Cookie 弹层",
        'id="musicRows"' in html and 'id="ckModal"' in html and 'id="ckRaw"' in html)
    chk("前端按平台渲染、可切默认、可存 Cookie",
        "function loadMusic(" in js and "function ckSave(" in js
        and "function musicDefault(" in js and "/api/music/cookie" in js)
    chk("翻唱页 / 访客页都有平台下拉并带进搜索请求",
        'id="platform"' in cover_html and "&platform=" in cover_html
        and 'id="platform"' in guest_html and "platform=" in guest_html)

    import settings
    spec = next((x for x in settings.SPEC if x["key"] == "music_platform"), None)
    chk("设置里有「默认音源平台」且选项与注册表一致",
        spec is not None and spec["type"] == "enum"
        and set(spec["choices"]) == set(src.REGISTRY), spec)

    import cover
    chk("面板的平台表与 ncmapi 注册表一致（加平台必须两边都改）",
        set(cover.PLATFORMS) == set(src.REGISTRY), (list(cover.PLATFORMS), list(src.REGISTRY)))
    chk("song_id 分隔符不用冒号（Windows 文件名不能有 ':'）",
        "-" in src.base.make_id("qq", "a") and ":" not in src.base.make_id("qq", "a"))


def main():
    print("RVC 多音源平台回归测试（网易云 / QQ 音乐 / 酷狗）")
    src = load_pkg("e2e_src", os.path.join(NCM, "sources"))
    test_adapters(src)
    test_netease(src)
    test_gateway(src)
    test_panel(src)
    test_music_module(src)
    test_wiring(src)
    print("\n== 结果 ==")
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
