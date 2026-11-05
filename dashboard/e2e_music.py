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

        # --- 聚合搜索（v2.3.0）---
        real_all, real_acc = gw.sources.search_all, gw.sources.accounts
        gw.sources.search_all = lambda kw, limit=12: ([
            {"id": "X1", "name": kw, "artist": "A", "album": "", "duration": 100,
             "album_id": "", "platform": "qq", "platform_label": "QQ 音乐"}], [])
        gw.sources.accounts = lambda p, force=False: [
            {"id": "u111", "file": "u111.txt", "legacy": False, "valid": True,
             "login": True, "nickname": "QQ 123", "vip": False, "msg": ""}]
        try:
            d = get("/search?keywords=hi&platform=all")
            chk("网关：platform=all 走聚合，每行带来源平台",
                d["platform"] == "all" and d["songs"][0]["platform"] == "qq"
                and d["songs"][0]["platform_label"] == "QQ 音乐", d.get("songs"))
            d = get("/accounts?platform=qq")
            chk("网关：/accounts 列账号明细（面板退出按钮靠它）",
                d["count"] == 1 and d["accounts"][0]["id"] == "u111"
                and d["label"] == "QQ 音乐", d)
            d = get("/platforms")
            chk("网关：/platforms 每家带账号数", all("accounts" in p for p in d["platforms"]), d)
        finally:
            gw.sources.search_all, gw.sources.accounts = real_all, real_acc
    finally:
        gw.sources.search, gw.sources.song_url, gw.sources.login_status = (
            real_search, real_url, real_login)
        srv.shutdown()


# =====================================================================
# C. 面板接线
# =====================================================================
# A2. 账号池（同一平台存多个 Cookie）
# =====================================================================
def test_accounts(src):
    print("\n== A2. 账号池（同一平台多个 Cookie）==")
    base = src.base
    tmp = tempfile.mkdtemp()
    real_dir = base.COOKIE_DIR
    base.COOKIE_DIR = tmp
    try:
        book = base.CookieBook("qq", ("qm_keyst=",))
        book.save("uin=o111; qm_keyst=aa")
        book.save("uin=o222; qm_keyst=bb")
        ids = [x["id"] for x in book.accounts()]
        chk("存两个账号 → 两条记录，两个都能取流",
            sorted(ids) == ["u111", "u222"] and len(book.entries()) == 2, ids)

        book.save("uin=o111; qm_keyst=zz")
        chk("同一账号再存 = 覆盖（重登不产生重复条目）",
            len(book.accounts()) == 2 and book.get("u111")["cookie"].endswith("zz"),
            [x["cookie"][-10:] for x in book.accounts()])

        chk("删一个账号只动它自己",
            book.remove("u222") is True and [x["id"] for x in book.accounts()] == ["u111"]
            and not os.path.exists(os.path.join(tmp, "qq", "u222.txt")))
        chk("删不存在的账号返回 False（不抛错）", book.remove("nope") is False)
        chk("清空 = 这个平台的账号全删", book.clear() >= 1 and book.accounts() == [])

        # legacy：v2.1.x 的 ncm_cookie.txt / v2.2.0 的 cookies/<key>.txt 都要继续能用
        with open(os.path.join(tmp, "qq.txt"), "w", encoding="utf-8") as f:
            f.write("uin=o999; qm_keyst=legacy")
        book2 = base.CookieBook("qq", ("qm_keyst=",))
        book2.save("uin=o888; qm_keyst=pool")
        first = book2.accounts()[0]
        chk("旧版单文件被当成 legacy 账号、且排在池前面",
            first["id"] == "legacy" and first["legacy"] is True
            and len(book2.accounts()) == 2, [x["id"] for x in book2.accounts()])
        chk("退出 legacy = 删掉那个旧文件（网易云就是 ncm_cookie.txt）",
            book2.remove("legacy") is True
            and not os.path.exists(os.path.join(tmp, "qq.txt"))
            and [x["id"] for x in book2.accounts()] == ["u888"])

        # 文件名安全：ident 最终要当文件名，绝不能借 ../ 写到池外
        chk("账号 id 落到文件名前先清洗（不许带路径分隔符）",
            "/" not in base.safe_id("../../evil") and "\\" not in base.safe_id("..\\..\\evil")
            and ".." not in base.safe_id("../../evil"), base.safe_id("../../evil"))
        book3 = base.CookieBook("qq", ("qm_keyst=",))
        book3.save("uin=o777; qm_keyst=x", ident="../../escape")
        chk("带 ../ 的 ident 仍然只落在池目录里",
            os.path.isfile(os.path.join(tmp, "qq", "escape.txt")),
            os.listdir(os.path.join(tmp, "qq")))

        # 轮换
        seen = []
        def fn(ck):
            seen.append(ck)
            return "URL" if ck.endswith("2") else ""
        res, used = base.try_accounts([("a", "c1"), ("b", "c2")], fn)
        chk("取流轮换：第一个拿不到就换下一个",
            res == "URL" and used == "b" and seen == ["c1", "c2"], (res, used, seen))
        try:
            base.try_accounts([("a", "c1"), ("b", "c2")], lambda ck: "")
            err = None
        except base.SourceError as e:
            err = e
        chk("全失败时把每个账号的原因带出来（不只说「失败」）",
            err is not None and "a：" in str(err) and "b：" in str(err), err)
        try:
            base.try_accounts([], lambda ck: "x")
            err2 = None
        except base.SourceError as e:
            err2 = e
        chk("一个账号都没有 → need_login=True（界面据此提示去配）",
            err2 is not None and err2.need_login is True, err2)
    finally:
        base.COOKIE_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# A3. 取流按账号轮换（三家各自的失败语义）
# =====================================================================
def test_rotate(src):
    print("\n== A3. 取流按账号轮换 ==")
    base, qq, kg, ne = src.base, src.qq, src.kugou, src.netease
    tmp = tempfile.mkdtemp()
    real_dir = base.COOKIE_DIR
    base.COOKIE_DIR = tmp
    keep = (qq.STORE, ne.STORE, kg.STORE)
    keep_fn = (qq.http_post_json, ne.http_get_json, kg.http_get_json)
    try:
        # QQ：第一个账号 purl 空（VIP 曲 / 过期），第二个有
        qb = base.CookieBook("qq", ("qm_keyst=",))
        qb.save("uin=o111; qm_keyst=aa")
        qb.save("uin=o222; qm_keyst=bb")
        qq.STORE = qb
        cnt = {"n": 0}
        def post(url, body, headers=None, timeout=12):
            cnt["n"] += 1
            purl = "M500.mp3?vkey=9" if cnt["n"] > 1 else ""
            return {"req_0": {"data": {"sip": ["http://dl/"],
                                       "midurlinfo": [{"purl": purl}]}}}
        qq.http_post_json = post
        url = qq.song_url("0035GveV3i9dBM")
        chk("QQ：第一个账号没直链就自动换下一个",
            url == "http://dl/M500.mp3?vkey=9" and cnt["n"] == 2, (url, cnt["n"]))

        # 网易云：第一个账号 code=404（没版权），第二个有
        nb = base.CookieBook("netease", ("MUSIC_U=",))
        nb.save("MUSIC_U=aaa111; os=pc")
        nb.save("MUSIC_U=bbb222; os=pc")
        ne.STORE = nb
        c2 = {"n": 0}
        def neget(url, headers=None, timeout=12):
            c2["n"] += 1
            if c2["n"] == 1:
                return {"data": [{"url": "", "code": 404}], "code": 404}
            return {"data": [{"url": "http://m/2.mp3", "code": 200}]}
        ne.http_get_json = neget
        chk("网易云：第一个账号没版权就换下一个",
            ne.song_url("12345") == "http://m/2.mp3" and c2["n"] == 2, c2["n"])

        # 酷狗：第一个账号是付费曲（status=2），第二个免费
        kb = base.CookieBook("kugou", ("token=", "userid="))
        kb.save("userid=111; token=t1")
        kb.save("userid=222; token=t2")
        kg.STORE = kb
        c3 = {"n": 0}
        def kget(url, headers=None, timeout=12):
            c3["n"] += 1
            if c3["n"] == 1:
                return {"status": 2, "url": []}
            return {"status": 1, "url": ["http://kg/2.mp3"]}
        kg.http_get_json = kget
        chk("酷狗：第一个账号付费拿不到就换账号（且不白试三种音质）",
            kg.song_url("5146461A5717") == "http://kg/2.mp3" and c3["n"] == 2, c3["n"])
    finally:
        qq.STORE, ne.STORE, kg.STORE = keep
        qq.http_post_json, ne.http_get_json, kg.http_get_json = keep_fn
        base.COOKIE_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)


# =====================================================================
# B2. 聚合搜索
# =====================================================================
def test_aggregate(src):
    print("\n== B2. 聚合搜索 ==")
    import cover
    mods = src.REGISTRY
    real = {k: mods[k].search for k in src.ORDER}
    try:
        mods["netease"].search = lambda kw, limit=12, offset=0: [
            {"id": "1", "name": "n1", "artist": "a", "album": "", "duration": 100}]
        mods["qq"].search = lambda kw, limit=12, offset=0: [
            {"id": "A", "name": "q1", "artist": "b", "album": "", "duration": 100},
            {"id": "B", "name": "q2", "artist": "b", "album": "", "duration": 100}]
        mods["kugou"].search = lambda kw, limit=12, offset=0: []
        rows, errs = src.search_all("x", 6)
        chk("聚合：三家一起搜，结果按平台交错（不是一家排满再下一家）",
            [(r["platform"], r["name"]) for r in rows]
            == [("netease", "n1"), ("qq", "q1"), ("qq", "q2")] and errs == [],
            [(r["platform"], r["name"]) for r in rows])
        chk("聚合：每行自带来源平台名",
            rows[1]["platform_label"] == "QQ 音乐", rows[1])

        def boom(kw, limit=12, offset=0):
            raise RuntimeError("上游 502")
        mods["kugou"].search = boom
        rows2, errs2 = src.search_all("x", 6)
        chk("聚合：一家挂了不影响另外两家，失败原因带平台名",
            len(rows2) == 3 and errs2 and "酷狗" in errs2[0], errs2)
    finally:
        for k, fn in real.items():
            mods[k].search = fn

    old_get, old_agg = cover._json_get, cover.aggregate_on
    stub = lambda url, timeout=25: {
        "songs": [
            {"id": "1", "name": "n1", "platform": "netease", "duration": 100},
            {"id": "A", "name": "q1", "platform": "qq", "duration": 100, "paid": True},
            {"id": "H", "name": "k1", "platform": "kugou", "duration": 100}],
        "errors": []}
    cover._json_get = stub
    try:
        cover.aggregate_on = lambda: True
        r = cover.search("x", 6, "")
        chk("面板：聚合开着且没选平台 → 走 platform=all，id 按各行来源加前缀",
            r["aggregate"] is True and r["platform"] == "all"
            and [x["id"] for x in r["songs"]] == ["netease-1", "qq-A", "kugou-H"],
            [x["id"] for x in r["songs"]])
        chk("面板：付费曲带 paid 标记（界面提前显示 VIP，别等点进去才失败）",
            r["songs"][1]["paid"] is True and r["songs"][0]["paid"] is False,
            [(x["id"], x["paid"]) for x in r["songs"]])
        r2 = cover.search("x", 6, "qq")
        chk("面板：明确选了平台就不聚合（临时换一家必须生效）",
            r2["aggregate"] is False and r2["platform"] == "qq", r2.get("platform"))
        cover.aggregate_on = lambda: False
        r3 = cover.search("x", 6, "")
        chk("面板：关掉聚合就只搜默认平台",
            r3["aggregate"] is False and r3["platform"] == cover.default_platform(),
            r3.get("platform"))
    finally:
        cover._json_get, cover.aggregate_on = old_get, old_agg

    rows = []
    for i in range(4):
        for p in ("netease", "qq", "kugou"):
            rows.append({"platform": p, "id": "%s%d" % (p, i)})
    keep = cover._trim_interleaved(rows, 6)
    chk("聚合结果截断：limit=6 时每家各留 2 首（不让第一家吃满）",
        len(keep) == 6 and all(
            sum(1 for x in keep if x["platform"] == p) == 2
            for p in ("netease", "qq", "kugou")),
        [(x["platform"], x["id"]) for x in keep])


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


# =====================================================================
# A4. 扫码登录（三家统一入口；不联网，QrLogin 全部打桩）
# =====================================================================
def test_qr(src):
    print("\n== A4. 扫码登录（会话 / 落盘 / 状态码）==")
    base = src.base
    tmp = tempfile.mkdtemp()
    real_dir = base.COOKIE_DIR
    base.COOKIE_DIR = tmp
    keep = (src.netease.QrLogin, src.qq.QrLogin, src.kugou.QrLogin,
            src.netease.STORE, src.qq.STORE, src.kugou.STORE)
    fakes = {}

    class FakeQr(object):
        """start/poll 都从外面喂，模拟「申请 → 等待 → 已扫 → 成功」全流程。"""
        plan = []                       # [(state, cookie), ...] 逐次弹出

        def start(self):
            return {"key": "fakekey", "qr": "https://example/qr?k=fakekey"}

        def poll(self):
            if not self.plan:
                raise base.SourceError("FakeQr 的脚本演完了")
            return self.plan.pop(0)

    fakes["netease"] = type("Fne", (FakeQr,), {})
    fakes["qq"] = type("Fqq", (FakeQr,), {})
    fakes["kugou"] = type("Fkg", (FakeQr,), {})
    try:
        src.netease.QrLogin = fakes["netease"]
        src.qq.QrLogin = fakes["qq"]
        src.kugou.QrLogin = fakes["kugou"]
        # STORE 也一起换成指向 tmp 的：原装实例的 dir 在构造时就固定到了
        # 真实 cookies/ 目录，只改 base.COOKIE_DIR 拦不住它们（踩过：测试
        # 把扫码落盘写进仓库目录，污染了下一个用例的取流断言）
        for k in src.ORDER:
            setattr(getattr(src, k), "STORE",
                    base.CookieBook(k, src.store(k).markers))

        chk("三家都声明支持扫码（加第四家要有 QrLogin 才不给按钮）",
            all(src.qr_supported(k) for k in src.ORDER), src.ORDER)

        # QQ 全流程：801 → 802 → 803 且 Cookie 落进账号池
        fakes["qq"].plan = [(801, ""), (802, ""), (803, "uin=o555; qm_keyst=zz")]
        r0 = src.qr_start("qq")
        chk("扫码会话：start 返回 token + App 指引，不把平台密钥给前端",
            r0.get("ok") and r0.get("key") and r0.get("hint") and "fakekey" not in r0["key"],
            {k: r0.get(k) for k in ("ok", "key", "hint")})
        p1 = src.qr_poll("qq", r0["key"])
        p2 = src.qr_poll("qq", r0["key"])
        p3 = src.qr_poll("qq", r0["key"])
        chk("轮询状态码：801 等待 → 802 已扫 → 803 成功",
            (p1["code"], p2["code"], p3["code"]) == (801, 802, 803),
            (p1.get("code"), p2.get("code"), p3.get("code")))
        chk("803 后 Cookie 已进账号池（扫码 = 添加一个账号）",
            any(x["id"] == "u555" for x in src.store("qq").accounts()),
            [x["id"] for x in src.store("qq").accounts()])
        chk("成功后会话被清掉：同一个 token 再轮询 → 800（不会重复落盘）",
            src.qr_poll("qq", r0["key"])["code"] == 800)

        # 酷狗：成功拿 token/userid；网易云：MUSIC_U
        fakes["kugou"].plan = [(803, "token=kk; userid=420000")]
        rk = src.qr_start("kugou")
        pk = src.qr_poll("kugou", rk["key"])
        chk("酷狗扫码 803：token/userid 存成账号（自动取名 u420000）",
            pk["ok"] and any(x["id"] == "u420000" for x in src.store("kugou").accounts()),
            (pk.get("code"), [x["id"] for x in src.store("kugou").accounts()]))
        fakes["netease"].plan = [(803, "MUSIC_U=ccc333")]
        rn = src.qr_start("netease")
        pn = src.qr_poll("netease", rn["key"])
        chk("网易云扫码 803：同样进账号池（不再是只有一个 legacy）",
            pn["ok"] and any(x["id"] == "nccc333" for x in src.store("netease").accounts()),
            [x["id"] for x in src.store("netease").accounts()])

        # 失败路径
        fakes["qq"].plan = [(803, "")]
        rq = src.qr_start("qq")
        pq = src.qr_poll("qq", rq["key"])
        chk("登录成功但没拿到 Cookie：明确说改用粘贴（不假装成功）",
            pq["ok"] is False and "粘贴" in pq.get("msg", ""), pq)
        fakes["kugou"].plan = [(800, "")]
        rk2 = src.qr_start("kugou")
        pk2 = src.qr_poll("kugou", rk2["key"])
        chk("二维码过期：返回 800 且会话清掉",
            pk2["code"] == 800 and src.qr_poll("kugou", rk2["key"])["code"] == 800, pk2)

        # token 随便编 / 平台不认识
        chk("编造的 token 当作二维码已失效（800），不抛 500",
            src.qr_poll("netease", "made-up")["code"] == 800)
        try:
            src.qr_start("nope")
            err = None
        except base.SourceError as e:
            err = e
        chk("不认识的平台：扫码直接拒绝（不拿网易云的码冒充）",
            err is not None and "nope" in str(err) and "netease" in str(err), err)

        # 过期会话回收（防进程里堆几十个没人领的会话）
        tok = "old-old-old"
        src._QR_SESS[tok] = ("qq", fakes["qq"](), 0.0)      # 1970 年的会话
        src._qr_trim()
        chk("过期扫码会话被回收（TTL / 上限）", tok not in src._QR_SESS)
    finally:
        (src.netease.QrLogin, src.qq.QrLogin, src.kugou.QrLogin,
         src.netease.STORE, src.qq.STORE, src.kugou.STORE) = keep
        src._QR_SESS.clear()
        base.COOKIE_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)

    # 签名 / token 算法对拍（离线）：这两处错了扫码会直接挂，值要钉死
    chk("酷狗 web 签名算法与已知值一致（盐 + 排序参数 + 盐）",
        src.kugou.web_sig({"appid": 1005, "clientver": 20489, "clienttime": 1700000000,
                           "dfid": "-", "mid": "abc", "uuid": "-", "plat": 4,
                           "srcappid": 2919, "qrcode": "K1"})
        == "9309f68e07ee99ef22c0958f4d71f62c",
        src.kugou.web_sig({"appid": 1005, "clientver": 20489, "clienttime": 1700000000,
                           "dfid": "-", "mid": "abc", "uuid": "-", "plat": 4,
                           "srcappid": 2919, "qrcode": "K1"}))
    chk("QQ hash33 / g_tk 算法与已知值一致",
        src.qq.hash33("abc") == 108966 and src.qq.gtk_of("abc") == 193485963,
        (src.qq.hash33("abc"), src.qq.gtk_of("abc")))


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

        def _offline(*a, **kw):
            raise RuntimeError("offline（测试不联网）")
        for _k in ("netease", "qq", "kugou"):
            m = getattr(mod, _k)
            m.http_get_json = _offline
            if hasattr(m, "http_post_json"):
                m.http_post_json = _offline
            setattr(m, "login_status",
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

        r2 = music.save_cookie("netease", "MUSIC_U=def")
        chk("再粘一个账号 → 变成 2 个（不是互相覆盖）",
            r2["ok"] and r2["count"] == 2 and "已添加账号" in r2["msg"], r2)
        r3 = music.save_cookie("netease", "MUSIC_U=abc")
        chk("同一个账号再粘 → 覆盖，不重复计数",
            r3["count"] == 2 and "已更新账号" in r3["msg"], r3)

        st = music.status(force=True)
        ne_row = next(p for p in st["platforms"] if p["key"] == "netease")
        chk("状态里带逐账号明细（概览页靠它渲染「退出」按钮）",
            ne_row["count"] == 2 and len(ne_row["accounts"]) == 2
            and all(a.get("id") for a in ne_row["accounts"]), ne_row.get("accounts"))

        who = ne_row["accounts"][0]["id"]
        out = music.logout("netease", who)
        chk("退出一个账号：只少一个，其余继续可用",
            out["ok"] and out["left"] == 1 and out["removed"] is True, out)
        out2 = music.logout("netease", who)
        chk("重复退出同一个账号不报错（幂等）",
            out2["ok"] and out2["removed"] is False, out2)
        out3 = music.clear_cookie("netease")
        chk("清空 = 这个平台的账号全删",
            out3["ok"] and out3["removed"] == 1 and out3["removed"], out3)
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

    # --- v2.3.0：多账号 / 退出 / 聚合 ---
    ov_js = open(os.path.join(HERE, "assets", "page_overview.js"), encoding="utf-8").read()
    ov_css = open(os.path.join(HERE, "assets", "console.css"), encoding="utf-8").read()
    post_src = open(os.path.join(HERE, "panelkit", "routes_post.py"), encoding="utf-8").read()
    set_js = open(os.path.join(HERE, "assets", "page_settings.js"), encoding="utf-8").read()
    chk("概览页能渲染账号列表并逐个退出",
        "function accRow(" in ov_js and "function accLogout(" in ov_js
        and "p.accounts" in ov_js and "/api/music/logout" in ov_js
        and ".maccs" in ov_css and ".acc " in ov_css)
    chk("面板有「退出单个账号」的路由（不是只清整个平台）",
        '"logout"' in post_src and "music.logout(" in post_src
        and '"cookie/clear"' in post_src)
    chk("翻唱页 / 访客页的下拉都有「全部平台」且按聚合开关预选",
        '<option value="all">' in cover_html and "j.aggregate ? 'all'" in cover_html
        and "d.aggregate ? 'all'" in guest_html)
    agg = next((x for x in settings.SPEC if x["key"] == "music_aggregate"), None)
    chk("设置里有「聚合搜索」开关，且默认开",
        agg is not None and agg["type"] == "bool" and agg["default"] is True, agg)
    chk("布尔设置能收 true/false/1/0，乱写回落默认",
        settings._coerce(agg, "0")[0] is False and settings._coerce(agg, True)[0] is True
        and settings._coerce(agg, "yes")[0] is True
        and settings._coerce(agg, "乱写")[0] is True)
    chk("设置页把布尔项画成勾选框（否则会渲染成数字输入框）",
        "f.type === 'bool'" in set_js and "el.type === 'checkbox'" in set_js)
    chk("搜索结果给付费曲打 VIP 标记（翻唱页 + 访客页）",
        "s.paid?" in cover_html and "VIP" in cover_html
        and "s.paid?" in guest_html and "VIP" in guest_html)

    # --- v2.4.0：三家统一扫码登录 ---
    post_src = open(os.path.join(HERE, "panelkit", "routes_post.py"), encoding="utf-8").read()
    get_src = open(os.path.join(HERE, "panelkit", "routes_get.py"), encoding="utf-8").read()
    music_src = open(os.path.join(HERE, "panelkit", "music.py"), encoding="utf-8").read()
    init_src = open(os.path.join(NCM, "sources", "__init__.py"), encoding="utf-8").read()
    chk("概览页是通用扫码弹层（qrModal），旧网易云专属弹层已退场",
        'id="qrModal"' in html and 'id="qrBox"' in html and "function qrOpen(" in js
        and "function qrPoll(" in js and "ncmModal" not in html)
    chk("每家都有扫码 + 粘贴两个按钮（扫码不再是网易云专属）",
        "qrOpen('${jsq(p.key)}')" in js and "ckOpen('${jsq(p.key)}')" in js
        and "'📱 再扫一个号'" in js)
    chk("三家适配器都有 QrLogin，状态码统一 801/802/803/800",
        all(hasattr(src.get(k), "QrLogin") for k in src.ORDER)
        and src.qr_supported("qq") and src.qr_supported("kugou")
        and src.qr_supported("netease"))
    chk("扫码会话只在 sources 里管理（803 才落盘、TTL 回收）",
        "def qr_start(" in init_src and "def qr_poll(" in init_src
        and "_QR_TTL" in init_src and "_QR_SESS" in init_src)
    chk("面板扫码路由：申请 POST / 轮询 GET，轮询查同源",
        'elif act == "qr"' in post_src and "music.qr_start(" in post_src
        and 'path == "/api/music/qr"' in get_src and "music.qr_poll(" in get_src
        and 'path == "/api/music/qr"' in get_src)
    chk("面板侧会画网易云的 SVG（QQ/酷狗用上游图）",
        "def _qr_mod(" in music_src and "qr.to_svg(" in music_src
        and 'rvc_music_qr' in music_src)


def main():
    print("RVC 多音源平台回归测试（网易云 / QQ 音乐 / 酷狗）")
    src = load_pkg("e2e_src", os.path.join(NCM, "sources"))
    test_adapters(src)
    test_accounts(src)
    test_rotate(src)
    test_qr(src)
    test_netease(src)
    test_gateway(src)
    test_aggregate(src)
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
