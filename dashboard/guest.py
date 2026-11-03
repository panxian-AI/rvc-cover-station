# -*- coding: utf-8 -*-
"""访客预约翻唱 + 站长审批。

分工：
    /            访客页（公网可见）：搜歌 → 选模型 → 提交申请 → 等审批 → 下载成品
    /            （同一路径）本机打开则是控制台：审批、队列排序、服务开关都在这
    /cover       自助翻唱页（只本机）
    /panel       监控面板（只本机）

管理没有独立界面、也没有密码：panel.py 只认本机直连，公网与局域网一律 403。
为什么这样设计：
    公网暴露后，访客不该直接占用 GPU。访客只能"申请"，真正开跑由站长批准，
    再加一道人工闸门；同时记录来访者来源，方便判断是谁在用。

数据落在 dashboard\\guest_db.json：visits（访客）/ requests（申请）/ queue（队列）。
"""
import json
import os
import re
import secrets
import threading
import time
import traceback

import cover as cover_mod
import captcha as captcha_mod

try:
    import tts as tts_mod
except Exception as _e:
    tts_mod = None
    print("⚠️ 文字转语音未加载（tts.py 导入失败）: %s" % _e)

# 申请类型
TYPE_COVER = "cover"
TYPE_TTS = "tts"

# 申请状态：pending（等批）→ queued（已批准，排队等 GPU）→ running（制作中）→ done
#          rejected（未通过）/ error（制作失败，可重新批准）
STATE_PENDING = "pending"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_REJECTED = "rejected"
STATE_ERROR = "error"

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "guest_db.json")
MAX_VISITS = 400
MAX_REQ = 300
MAX_PENDING_PER_GID = 3      # 同一访客同时待批的申请上限
MAX_PENDING_PER_IP = 3       # 同一来源 IP 的上限（删 Cookie 换 gid 也绕不过）
MAX_TITLE = 80
MAX_ARTIST = 60
MAX_KW = 40
GUEST_COOKIE = "rvc_guest"
_GID_RE = re.compile(r"^[0-9a-f]{16}$")

# 提交频率：同 IP 1 小时最多 8 单（防脚本刷满待批队列、把正常访客挡在外面）
SUB_WINDOW = 3600
SUB_LIMIT = 8

# 搜索频率：同 IP 10 分钟最多 50 次（验证码之外的第二道闸）
SEARCH_WINDOW = 600
SEARCH_LIMIT = 50

# 访客接口的通用轻限流：同 IP 每分钟最多 120 次。
# 正常访客一个页面 5 秒才轮询 1 次（约 12 次/分钟），扫描器挨个试接口会撞上这道。
API_WINDOW = 60
API_LIMIT = 120

# ---------------- 白名单（免审批直通） ----------------
# 权益：命中白名单的访客提交申请后跳过审批，直接进入制作队列。
# 身份：gid + 设备指纹 + IP 三者**全部一致**才算命中，任何一项变了都降回普通访客。
# 威胁模型：批量刷白名单。所以申请侧每层都卡死（见 WL_* 常量与 wl_apply），
#          且只认 [0-9a-f]{32,64} 的指纹、只存净文本，没有任何拼接进 SQL/路径的地方
#          （存储本来就是 JSON 文件 + 精确等值比较，不存在注入面）。
WL_FP_RE = re.compile(r"^[0-9a-f]{32,64}$")
WL_REASON_MIN = 5            # 申请理由最少字数（太短的批量灌水没意义）
WL_REASON_MAX = 200          # 申请理由最多字数
WL_MAX_ENTRIES = 200         # 白名单容量上限
WL_MAX_PENDING = 30          # 全局待审白名单申请上限
WL_MAX_APPS = 300            # 申请记录保留条数
WL_APP_PER_IP_DAY = 3        # 同一 IP 24 小时内最多申请次数
WL_REJECT_COOLDOWN = 86400   # 被拒后同一访客 24 小时内不能再申请

_lock = threading.RLock()
_db = {"visits": [], "requests": [], "queue": [], "wl_apps": [], "whitelist": []}
_sub_hits = {}               # ip -> [时间戳]
_search_hits = {}            # ip -> [时间戳]
_api_hits = {}               # ip -> [时间戳]
_visit_ip = {}               # ip -> 最近一次新建访客记录的时间

# 队列节奏：一次只跑一单（GPU 只有一个），失败的暂停一会儿再自动往下推，
# 免得后端没起来的时候把整队申请连着打成 error。
PUMP_GAP = 5                 # 兜底巡队间隔（秒）
FAIL_COOLDOWN = 60           # 失败后暂停自动推进的秒数
_cooldown_until = 0.0


# ---------------- 存储 ----------------
def _load():
    global _db
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            wl_apps = d.get("wl_apps")
            wl_list = d.get("whitelist")
            _db = {"visits": d.get("visits") or [],
                   "requests": d.get("requests") or [],
                   "queue": [str(x) for x in (d.get("queue") or [])],
                   # 老库没有白名单字段 → 空表，别让 KeyError
                   "wl_apps": wl_apps if isinstance(wl_apps, list) else [],
                   "whitelist": wl_list if isinstance(wl_list, list) else []}
    except Exception:
        _db = {"visits": [], "requests": [], "queue": [], "wl_apps": [], "whitelist": []}
    return _db


_dirty = False                # 有未落盘的改动（soft 写被节流时会置上）
_last_save = 0.0
SAVE_GAP = 2.0                # soft 写最小间隔（秒）


def _save(soft=False):
    """把 _db 落盘。

    soft=True 用于「访客来访计数 / 搜索计数」这类高频、丢一两条无所谓的写入：
    距上次写盘不足 SAVE_GAP 秒就只打个脏标记，交给巡队线程补写。
    为什么需要：每次写盘都是整份 JSON（现在 100KB+），而且是在 _lock 里做的 ——
    公网被刷时（每个新 IP 一条访客记录）会把所有请求串行卡在这上面。
    """
    global _dirty, _last_save
    now = time.time()
    with _lock:
        _dirty = True
        if soft and now - _last_save < SAVE_GAP:
            return
        _last_save = now
        _dirty = False
        try:
            tmp = DB_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_db, f, ensure_ascii=False, indent=1)
            os.replace(tmp, DB_PATH)
        except OSError:
            pass


def flush():
    """把被节流压住的 soft 写补上（巡队线程定时调）。"""
    with _lock:
        if not _dirty:
            return
    _save()


_load()


# ---------------- 访客身份 ----------------
def gid_from(cookie_header):
    """从 Cookie 取访客 id；没有或不合法就生成新的。返回 (gid, 是否新建)。

    只认 16 位小写十六进制。客户端可以随手改这个 Cookie，所以它**不是身份凭证**，
    真正拦刷单的是按 IP 的限制；这里只保证落库/渲染的值不会被塞进奇怪内容。
    """
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == GUEST_COOKIE:
            v = v.strip()
            if _GID_RE.match(v):
                return v, False
            break
    return secrets.token_hex(8), True


def gid_cookie(gid, secure=False):
    return "%s=%s; Path=/; Max-Age=%d; SameSite=Lax; HttpOnly%s" % (
        GUEST_COOKIE, gid, 30 * 24 * 3600, "; Secure" if secure else "")


def _short_ua(ua):
    ua = ua or ""
    os_name = "未知设备"
    for k, v in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                 ("Windows", "Windows"), ("Macintosh", "macOS"), ("Linux", "Linux")):
        if k in ua:
            os_name = v
            break
    app = ""
    if "MicroMessenger" in ua:
        app = "微信"
    elif "Edg/" in ua:
        app = "Edge"
    elif "Firefox/" in ua:
        app = "Firefox"
    elif "Chrome/" in ua:
        app = "Chrome"
    elif "Safari/" in ua:
        app = "Safari"
    elif "python" in ua.lower() or "curl" in ua.lower():
        app = "脚本"
    return (os_name + ("·" + app if app else "")).strip("·")


def touch(gid, ip, country, ua):
    """记录一次来访（同一访客合并计数）。

    每个请求都重写整份 JSON 太费盘，所以：同一访客 30 秒内只更新一次；
    同一 IP 60 秒内不重复新建访客条目（否则刷 / 就能把 visits 表刷满并反复写盘）。
    """
    now = time.time()
    with _lock:
        for v in _db["visits"]:
            if v["id"] == gid:
                if now - float(v.get("last") or 0) < 30:
                    return v
                v["last"] = now
                v["count"] = int(v.get("count", 1)) + 1
                v["ip"] = ip or v.get("ip")
                v["country"] = country or v.get("country")
                v["ua"] = _short_ua(ua) or v.get("ua")
                _save(soft=True)
                return v
        if ip and now - float(_visit_ip.get(ip) or 0) < 60:
            # 同一 IP 短时间内反复换 Cookie 来"新访客"——只算一次
            for v in _db["visits"]:
                if v.get("ip") == ip:
                    v["last"] = now
                    v["count"] = int(v.get("count", 1)) + 1
                    _save(soft=True)
                    return v
        v = {"id": gid, "ip": ip or "", "country": country or "",
             "ua": _short_ua(ua), "raw_ua": (ua or "")[:200],
             "first": now, "last": now, "count": 1}
        _db["visits"].insert(0, v)
        del _db["visits"][MAX_VISITS:]
        if ip:
            _visit_ip[ip] = now
            if len(_visit_ip) > 5000:
                for k in list(_visit_ip)[:2500]:
                    _visit_ip.pop(k, None)
        _save(soft=True)
        return v


def touch_search(gid):
    """记一次成功搜索（给审批台看活跃度）。"""
    with _lock:
        for v in _db["visits"]:
            if v["id"] == gid:
                v["searches"] = int(v.get("searches", 0)) + 1
                _save(soft=True)
                return


def _search_allowed(ip):
    """搜索总闸：同一 IP 10 分钟最多 50 次。

    验证码解决「是不是人」，这道闸解决「一次通过后能刷多少」——
    通行证本身限 12 次，这里再兜一层，防止有人开一堆通行证轮着用。
    """
    if not ip:
        return True, ""
    now = time.time()
    with _lock:
        hits = [t for t in _search_hits.get(ip, []) if now - t < SEARCH_WINDOW]
        if len(hits) >= SEARCH_LIMIT:
            _search_hits[ip] = hits
            return False, "搜索太频繁了，休息一会再搜"
        hits.append(now)
        _search_hits[ip] = hits
        if len(_search_hits) > 2000:
            for k in list(_search_hits)[:1000]:
                _search_hits.pop(k, None)
    return True, ""


def _fmt(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""


def visits():
    with _lock:
        items = []
        for v in _db["visits"]:
            items.append({
                "id": v["id"], "ip": v.get("ip", ""), "country": v.get("country", ""),
                "ua": v.get("ua", ""), "count": int(v.get("count", 1)),
                "searches": int(v.get("searches", 0)),
                "first": _fmt(v.get("first")), "last": _fmt(v.get("last")),
                "last_ts": v.get("last", 0),
                "requests": sum(1 for r in _db["requests"] if r.get("gid") == v["id"]),
            })
        items.sort(key=lambda x: x["last_ts"], reverse=True)
    for it in items:
        it.pop("last_ts", None)
    return {"ok": True, "visits": items, "total": len(items)}


# ---------------- 申请 ----------------
def _req_view(r, admin=False):
    out = {
        "id": r["id"], "type": r.get("type", TYPE_COVER),
        "title": r.get("title", ""), "artist": r.get("artist", ""),
        "song_id": r.get("song_id", ""), "model": r.get("model", ""),
        "voice": r.get("voice", ""), "acapella": bool(r.get("acapella")),
        "state": r.get("state", "pending"), "created": _fmt(r.get("created")),
        "created_ts": r.get("created", 0), "note": r.get("note", ""),
        "error": r.get("error", ""), "file": r.get("file") or "",
        "size_text": r.get("size_text", ""), "reuse": bool(r.get("reuse")),
    }
    # 排队中的单带上位次，访客和站长都能看到「前面还有几单」
    if r.get("state") == STATE_QUEUED:
        try:
            q = _db.get("queue") or []
            out["q_pos"] = q.index(r["id"]) + 1 if r["id"] in q else 0
            out["q_total"] = len(q)
        except Exception:
            out["q_pos"] = 0
            out["q_total"] = 0
    text = r.get("text") or ""
    out["text"] = text[:200] if not admin else text
    # 朗读的显示名：套自己声线时显示「小肥鱼（肥鱼声库）」而不是 rvc:feiyu.pth
    if out["type"] == TYPE_TTS:
        v = out.get("voice") or ""
        if v.startswith("rvc:") and tts_mod:
            out["voice_label"] = tts_mod.model_label(out.get("model") or v[4:])
        else:
            out["voice_label"] = v.replace("Neural", "")
    if admin:
        out.update({"ip": r.get("ip", ""), "country": r.get("country", ""),
                    "ua": r.get("ua", ""), "gid": r.get("gid", ""),
                    "auto": bool(r.get("auto")),
                    "decided": _fmt(r.get("decided"))})
    return out


def _valid_model(model):
    """模型名必须是 weights 目录里真实存在的 .pth，且不允许带路径。"""
    model = os.path.basename(str(model or "").strip())
    if not model.lower().endswith(".pth"):
        return ""
    p = os.path.join(cover_mod.WEIGHTS_DIR, model)
    return model if os.path.isfile(p) else ""


def _api_allowed(ip):
    """访客接口通用轻限流（每分钟 120 次）。"""
    if not ip:
        return True
    now = time.time()
    with _lock:
        hits = [t for t in _api_hits.get(ip, []) if now - t < API_WINDOW]
        if len(hits) >= API_LIMIT:
            _api_hits[ip] = hits
            return False
        hits.append(now)
        _api_hits[ip] = hits
        if len(_api_hits) > 3000:
            for k in list(_api_hits)[:1500]:
                _api_hits.pop(k, None)
    return True


def _sub_allowed(ip):
    """同 IP 提交频率限制。返回 (ok, 提示)。"""
    if not ip:
        return True, ""
    now = time.time()
    with _lock:
        hits = [t for t in _sub_hits.get(ip, []) if now - t < SUB_WINDOW]
        _sub_hits[ip] = hits
        if len(_sub_hits) > 5000:
            for k in list(_sub_hits)[:2500]:
                _sub_hits.pop(k, None)
        if len(hits) >= SUB_LIMIT:
            return False, "提交太频繁了，过一会儿再来"
        hits.append(now)
        return True, ""


def create_request(gid, ip, country, ua, payload):
    """统一入口：按 type 分流到 翻唱 / 文字转语音。payload.fp 是设备指纹（白名单用）。"""
    payload = payload or {}
    fp = str(payload.get("fp") or "").strip().lower()
    rtype = str(payload.get("type") or TYPE_COVER).strip()
    if rtype == TYPE_TTS:
        if not tts_mod:
            return {"ok": False, "msg": "文字转语音功能未启用"}
        return _create(gid, ip, country, ua, payload, rtype,
                       _check_tts(payload), fp)
    return _create(gid, ip, country, ua, payload, rtype, _check_cover(payload), fp)


def _check_cover(payload):
    song_id = str(payload.get("song_id") or "").strip()
    title = str(payload.get("title") or "").strip()[:MAX_TITLE]
    artist = str(payload.get("artist") or "").strip()[:MAX_ARTIST]
    model = _valid_model(payload.get("model"))
    if not song_id.isdigit() or len(song_id) > 20:
        return {"ok": False, "msg": "请先搜索并选中一首歌"}
    if not title:
        return {"ok": False, "msg": "歌曲信息不完整，请重新搜索"}
    if not model:
        return {"ok": False, "msg": "请选择一个声音模型"}
    return {"ok": True, "fields": {
        "song_id": song_id, "title": title, "artist": artist, "model": model,
        "acapella": bool(payload.get("acapella")), "text": "",
    }}


def _check_tts(payload):
    if not tts_mod:
        return {"ok": False, "msg": "文字转语音功能未启用"}
    v = tts_mod.validate(payload)
    if not v.get("ok"):
        return v
    return {"ok": True, "fields": {
        "song_id": "", "title": "朗读：" + v["title"],
        "artist": v["label"], "model": v["model"], "voice": v["voice"],
        "base": v["base"], "text": v["text"], "rate": v["rate"],
        "acapella": False,
    }}


def _create(gid, ip, country, ua, payload, rtype, chk, fp=""):
    if not chk.get("ok"):
        return {"ok": False, "msg": chk.get("msg", "参数不合法")}
    f = chk["fields"]

    oksub, why = _sub_allowed(ip)
    if not oksub:
        return {"ok": False, "msg": why}

    with _lock:
        _prune_zombies()
        # 白名单命中 → 这单直接批准入队（权益只跳过人工审批，防刷闸一个不少）
        auto_wl = is_whitelisted(gid, fp, ip)
        # 翻唱：同一访客同一首歌 + 同一模型，未完成时不重复排队
        if rtype == TYPE_COVER:
            for r in _db["requests"]:
                if (r.get("gid") == gid and r.get("song_id") == f["song_id"]
                        and r.get("model") == f["model"]
                        and r.get("state") in (STATE_PENDING, STATE_QUEUED, STATE_RUNNING)):
                    return {"ok": True, "msg": "这首已经在排队里了，等站长处理即可",
                            "request": _req_view(r)}
        # 翻唱：这个参数组合已经有成品了 → 批准后秒回，不用再占 GPU
        reuse_hit = None
        if rtype == TYPE_COVER:
            reuse_hit = cover_mod.find_cached(f["song_id"], f["model"], 0, f["acapella"])
        elif rtype == TYPE_TTS and tts_mod:
            try:
                reuse_hit = tts_mod._find_cached(
                    tts_mod.text_key(f.get("text", ""), f.get("voice", ""),
                                     f.get("base", ""), f.get("rate", "正常")))
            except Exception:
                reuse_hit = None

        pending_same = [r for r in _db["requests"] if r.get("state") == "pending"]
        if len(pending_same) >= 12:
            return {"ok": False, "msg": "当前待审批太多，稍后再试"}
        mine_pending = sum(1 for r in _db["requests"]
                           if r.get("gid") == gid
                           and r.get("state") in (STATE_PENDING, STATE_QUEUED, STATE_RUNNING))
        if mine_pending >= MAX_PENDING_PER_GID:
            return {"ok": False, "msg": "你还有 %d 单没做完，先等等再提交" % mine_pending}
        # 换 Cookie 就换 gid —— 再按 IP 卡一道，否则删 Cookie 就能无限排队
        if ip:
            ip_pending = sum(1 for r in _db["requests"]
                             if r.get("ip") == ip
                             and r.get("state") in (STATE_PENDING, STATE_QUEUED, STATE_RUNNING))
            if ip_pending >= MAX_PENDING_PER_IP:
                return {"ok": False, "msg": "这个网络还有 %d 单没做完，先等等再提交" % ip_pending}

        r = {"id": secrets.token_hex(6), "gid": gid, "ip": ip or "",
             "country": country or "", "ua": _short_ua(ua),
             "type": rtype if rtype in (TYPE_COVER, TYPE_TTS) else TYPE_COVER,
             "state": STATE_QUEUED if auto_wl else "pending",
             "created": time.time(),
             "decided": time.time() if auto_wl else None,
             "auto": bool(auto_wl),
             "job": None, "file": None, "error": None, "note": ""}
        r.update(f)
        _db["requests"].insert(0, r)
        del _db["requests"][MAX_REQ:]
        if auto_wl:
            # 命中成品库就直发（不占队列）；否则老老实实排队，再交给巡队线程推进
            if _cache_ready(r):
                res = _launch_locked(r)
                if not (res.get("ok") and r.get("state") == STATE_DONE):
                    r.update(state=STATE_QUEUED, job=None)
            if r.get("state") == STATE_QUEUED:
                _enqueue(r["id"])
        _save()
        view = _req_view(r)
    if auto_wl:
        _pump_async()
        if r.get("state") == STATE_DONE:
            return {"ok": True, "msg": "白名单直通：已有成品，直接可下载", "request": view}
        if r.get("state") == STATE_RUNNING:
            return {"ok": True, "msg": "白名单直通：已自动批准，马上开始制作", "request": view}
        return {"ok": True, "msg": "白名单直通：已自动批准，进入制作队列", "request": view}
    if reuse_hit:
        what = "这段文字这个音色" if rtype == TYPE_TTS else "这首歌这个声线"
        return {"ok": True, "reuse": True,
                "msg": "%s已经有成品（%s），站长批准后立刻可下载，不用等制作"
                       % (what, reuse_hit["size_text"]), "request": view}
    msg = "已提交，等站长批准后开始制作"
    return {"ok": True, "msg": msg, "request": view}


def my_requests(gid, ip="", alt_gid=""):
    """访客自己的申请列表。

    凭据只有一个：访客 id（gid，64 位随机十六进制）。
      · 优先用 Cookie 里的 gid；
      · Cookie 被清掉时，用前端 localStorage 里存着的 gid（请求参数带上来）找回。

    **绝不按 IP 兜底**：手机 4G、校园网、公司网都是成百上千人共用一个出口 IP，
    按 IP 取会把同网陌生人的申请（歌名、朗读原文）直接显示给别人。早期版本为了
    「换设备也能找回」这么干过，是实打实的越权读，已删掉。

    ip 参数保留只为兼容调用方，不再参与查询。
    """
    cands = [gid]
    alt = str(alt_gid or "").strip()
    if alt and alt != gid and _GID_RE.match(alt):
        cands.append(alt)

    use, items, recovered = gid, [], False
    for i, cand in enumerate(cands):
        if not cand or not _GID_RE.match(cand):
            continue
        with _lock:
            got = [_req_view(r) for r in _db["requests"] if r.get("gid") == cand]
        if got:
            use, items, recovered = cand, got, (i > 0)
            break
    items.sort(key=lambda x: x["created_ts"], reverse=True)
    return {"ok": True, "requests": items, "recovered": recovered, "gid": use}


def admin_requests(state="all"):
    with _lock:
        items = [_req_view(r, admin=True) for r in _db["requests"]]
    if state and state != "all":
        items = [i for i in items if i["state"] == state]
    items.sort(key=lambda x: x["created_ts"], reverse=True)
    counts = {}
    for i in items:
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    return {"ok": True, "requests": items, "counts": counts}


def _find(rid):
    for r in _db["requests"]:
        if r["id"] == rid:
            return r
    return None


def _prune_zombies():
    """把任务记录已丢失的 running（面板重启/进程中断造成）转成 error，释放名额。"""
    with _lock:
        changed = False
        for x in _db["requests"]:
            if x.get("state") != STATE_RUNNING:
                continue
            try:
                fn = tts_mod.job_status if x.get("type") == TYPE_TTS else cover_mod.job_status
                alive = fn(x.get("job") or "")
            except Exception:
                alive = None
            if not alive:
                x.update(state=STATE_ERROR, error="任务记录丢失（面板重启过），请重新提交或让站长再批")
                changed = True
        if changed:
            _save()


# ---------------- 白名单 ----------------
def _clean_reason(s):
    """申请理由只留可打印文本 + 换行，限长。控制字符/超长截断，杜绝任何注入面。"""
    s = str(s or "")
    s = "".join(ch for ch in s if ch == "\n" or ord(ch) >= 32)
    return s.strip()[:WL_REASON_MAX]


def _wl_entry_by(gid, fp=None, ip=None):
    """精确查找白名单条目；fp/ip 传 None 表示不参与匹配。"""
    with _lock:
        for w in _db.get("whitelist") or []:
            if w.get("gid") != gid:
                continue
            if fp is not None and w.get("fp") != fp:
                continue
            if ip is not None and (w.get("ip") or "") != (ip or ""):
                continue
            return w
    return None


def is_whitelisted(gid, fp, ip):
    """白名单命中 = gid + 设备指纹 + IP 三者全部一致。

    任何一项不一致都返回 False：换设备、换网络（WiFi↔流量）、清了 localStorage
    都会降回普通访客，需要重新申请。命中就顺手记一次使用次数（给站长看活跃度）。
    """
    fp = str(fp or "").strip().lower()
    if not gid or not WL_FP_RE.match(fp):
        return False
    with _lock:
        w = _wl_entry_by(gid, fp=fp, ip=ip or "")
        if not w:
            return False
        w["hits"] = int(w.get("hits", 0)) + 1
        w["last_seen"] = time.time()
        _save(soft=True)
        return True


def wl_mine(gid, fp, ip):
    """访客查自己的白名单状态。只回状态短语，不回别人任何信息。"""
    fp = str(fp or "").strip().lower()
    st, note = "none", ""
    with _lock:
        for w in _db.get("whitelist") or []:
            if w.get("gid") != gid:
                continue
            if w.get("fp") == fp and (w.get("ip") or "") == (ip or ""):
                st, note = "approved", "免审批直通已生效：提交申请后自动进制作队列"
            elif w.get("fp") == fp:
                st, note = "mismatch", "你的网络变了，白名单暂不生效；请站长重新批准一次即可"
            else:
                st, note = "mismatch", "设备或网络变了，白名单失效；需要重新申请"
            break
        if st == "none":
            has_pending, reject_note = False, ""
            for a in _db.get("wl_apps") or []:
                if a.get("gid") != gid:
                    continue
                if a.get("state") == "pending":
                    has_pending = True
                elif a.get("state") == "rejected" and not reject_note:
                    reject_note = a.get("note") or "站长没有通过这次申请"
            if has_pending:
                st, note = "pending", "申请已提交，等站长处理"
            elif reject_note:
                st, note = "rejected", reject_note
    return {"ok": True, "state": st, "note": note}


def wl_apply(gid, fp, ip, country, ua, reason):
    """提交白名单申请。防批量刷的每一道闸都在这里。"""
    fp = str(fp or "").strip().lower()
    reason = _clean_reason(reason)
    if not WL_FP_RE.match(fp):
        return {"ok": False, "msg": "页面版本太旧，刷新一下页面再试"}
    if len(reason) < WL_REASON_MIN:
        return {"ok": False, "msg": "多写几句申请理由（至少 %d 个字），方便站长认识你" % WL_REASON_MIN}
    now = time.time()
    with _lock:
        if _wl_entry_by(gid, fp=fp, ip=ip or ""):
            return {"ok": True, "msg": "你已经在白名单里了", "state": "approved"}
        # 同 gid+指纹已有记录但 IP 变了 → 允许重新申请（站长批一下就更新 IP），
        # 不能把人挡死在门外；批准时 wl_decide 会做幂等更新。
        # 注意：不能用 _db.get("wl_apps") or [] —— 空表时会拿到临时副本，插进去就丢
        apps = _db.setdefault("wl_apps", [])
        if any(a.get("gid") == gid and a.get("state") == "pending" for a in apps):
            return {"ok": False, "msg": "你已有一条待审的申请，等站长处理就好"}
        # 被拒冷却：不给脚本「被拒了马上再试」的机会
        if any(a.get("gid") == gid and a.get("state") == "rejected"
               and now - float(a.get("decided") or 0) < WL_REJECT_COOLDOWN for a in apps):
            return {"ok": False, "msg": "刚被拒绝过，明天再来申请"}
        # 同 IP 24h 次数：换 gid 也绕不过（IP 是服务端看到的，伪造不了）
        ipn = sum(1 for a in apps
                  if a.get("ip") == ip and now - float(a.get("created") or 0) < 86400)
        if ipn >= WL_APP_PER_IP_DAY:
            return {"ok": False, "msg": "这个网络今天申请的次数用完了，明天再来"}
        if sum(1 for a in apps if a.get("state") == "pending") >= WL_MAX_PENDING:
            return {"ok": False, "msg": "待审的申请太多，过两天再来"}
        if len(_db.get("whitelist") or []) >= WL_MAX_ENTRIES:
            return {"ok": False, "msg": "白名单已满，请联系站长"}
        a = {"id": secrets.token_hex(6), "gid": gid, "fp": fp, "ip": ip or "",
             "country": country or "", "ua": _short_ua(ua),
             "reason": reason, "created": now, "state": "pending",
             "decided": None, "note": ""}
        apps.insert(0, a)
        del apps[WL_MAX_APPS:]
        _save()
    return {"ok": True, "msg": "申请已提交，等站长处理", "state": "pending"}


def wl_admin_view():
    """控制台的白名单页：待审申请（带访客历史）+ 已批准名单。"""
    with _lock:
        apps = []
        for a in _db.get("wl_apps") or []:
            gid = a.get("gid", "")
            apps.append({
                "id": a.get("id", ""), "gid": gid,
                "fp_short": (a.get("fp") or "")[:12] + "…",
                "ip": a.get("ip", ""), "country": a.get("country", ""),
                "ua": a.get("ua", ""), "reason": a.get("reason", ""),
                "state": a.get("state", "pending"),
                "created": _fmt(a.get("created")),
                "created_ts": float(a.get("created") or 0),
                "decided": _fmt(a.get("decided")), "note": a.get("note", ""),
                # 给站长的人工判断材料：这个人历史申请多少、被拒多少
                "gid_reqs": sum(1 for r in _db["requests"] if r.get("gid") == gid),
                "gid_rejected": sum(1 for r in _db["requests"]
                                    if r.get("gid") == gid and r.get("state") == STATE_REJECTED),
                "already_wl": _wl_entry_by(gid, fp=a.get("fp")) is not None,
            })
        entries = []
        for w in _db.get("whitelist") or []:
            entries.append({
                "gid": w.get("gid", ""), "ip": w.get("ip", ""),
                "country": w.get("country", ""), "ua": w.get("ua", ""),
                "note": w.get("note", ""), "hits": int(w.get("hits", 0)),
                "approved": _fmt(w.get("approved")),
                "last_seen": _fmt(w.get("last_seen")),
                "last_ts": float(w.get("last_seen") or 0),
                "active": time.time() - float(w.get("last_seen") or 0) < 14 * 86400,
            })
        apps.sort(key=lambda x: x.get("created_ts") or 0, reverse=True)
        entries.sort(key=lambda x: x.pop("last_ts"), reverse=True)
        apps = [{k: v for k, v in a.items() if k != "created_ts"} for a in apps]
        pend = sum(1 for a in apps if a["state"] == "pending")
    return {"ok": True, "apps": apps, "entries": entries,
            "counts": {"pending": pend, "total": len(entries)}}


def wl_decide(app_id, action, note=""):
    """批准 / 拒绝白名单申请（只允许本机调用）。"""
    action = str(action or "").lower()
    if action not in ("approve", "reject"):
        return {"ok": False, "msg": "未知操作"}
    note = str(note or "")[:120]
    with _lock:
        a = None
        for x in _db.get("wl_apps") or []:
            if x.get("id") == app_id:
                a = x
                break
        if not a:
            return {"ok": False, "msg": "申请不存在"}
        if a.get("state") != "pending":
            return {"ok": False, "msg": "这条已处理过（当前：%s）" % a.get("state")}
        a["state"] = "approved" if action == "approve" else "rejected"
        a["decided"] = time.time()
        a["note"] = note
        if action == "approve":
            # 幂等：同 gid+指纹已有记录就只更新 IP（处理「换网络重新批」）
            hit = _wl_entry_by(a.get("gid"), fp=a.get("fp"))
            if hit:
                hit["ip"] = a.get("ip") or hit.get("ip")
                hit["country"] = a.get("country") or hit.get("country")
                hit["approved"] = time.time()
                hit["note"] = note
                msg = "已更新白名单（同设备新网络）"
            elif len(_db.get("whitelist") or []) >= WL_MAX_ENTRIES:
                a["state"] = "pending"
                a["decided"] = None
                return {"ok": False, "msg": "白名单已满，先移除一些再加"}
            else:
                _db["whitelist"].insert(0, {
                    "gid": a.get("gid", ""), "fp": a.get("fp", ""),
                    "ip": a.get("ip", ""), "country": a.get("country", ""),
                    "ua": a.get("ua", ""), "note": note,
                    "approved": time.time(), "hits": 0, "last_seen": 0})
                msg = "已加入白名单（该设备 + 该网络，申请免审批直接排队）"
        else:
            msg = "已拒绝这条申请"
        _save()
    return {"ok": True, "msg": msg}


def wl_revoke(gid):
    """把某个访客移出白名单（立即失效）。"""
    gid = str(gid or "")
    with _lock:
        before = len(_db.get("whitelist") or [])
        _db["whitelist"] = [w for w in _db.get("whitelist") or [] if w.get("gid") != gid]
        removed = before - len(_db["whitelist"])
        if removed:
            _save()
    return {"ok": removed > 0,
            "msg": ("已移出白名单（%d 条记录）" % removed) if removed else "没找到这条白名单"}



def _queue_alive():
    """整理队列：丢掉不存在/已不是 queued 的条目，把漏登记的 queued 申请补进来。"""
    q = [str(x) for x in (_db.get("queue") or [])]
    alive = {r["id"]: r for r in _db["requests"] if r.get("state") == STATE_QUEUED}
    out, seen = [], set()
    for rid in q:
        if rid in alive and rid not in seen:
            out.append(rid)
            seen.add(rid)
    # 状态是 queued 却没进队列（老数据 / 中途落盘失败）：按提交时间补到末尾
    for rid, r in sorted(alive.items(), key=lambda kv: float(kv[1].get("created") or 0)):
        if rid not in seen:
            out.append(rid)
            seen.add(rid)
    changed = out != q
    _db["queue"] = out
    return changed


def _enqueue(rid):
    """按提交时间插队：默认先到先来（站长手动挪过的顺序不会被后续插队打乱，只影响新批的这单）。"""
    r = _find(rid)
    if not r:
        return -1
    _queue_alive()
    q = _db["queue"]
    pos = len(q)
    created = float(r.get("created") or 0)
    for i, other in enumerate(q):
        ro = _find(other)
        if ro and float(ro.get("created") or 0) > created:
            pos = i
            break
    q.insert(pos, rid)
    return pos


def queue_list():
    """当前队列（含位置），给审批台显示。"""
    with _lock:
        _queue_alive()
        out = []
        for i, rid in enumerate(_db["queue"], 1):
            r = _find(rid)
            if not r:
                continue
            out.append({"pos": i, "id": rid, "title": r.get("title", ""),
                        "artist": r.get("artist", ""), "type": r.get("type", TYPE_COVER),
                        "created": _fmt(r.get("created")),
                        "acapella": bool(r.get("acapella")),
                        "model": os.path.basename(str(r.get("model") or "")),
                        "voice": r.get("voice", "")})
        running = [r for r in _db["requests"] if r.get("state") == STATE_RUNNING]
        return {"ok": True, "queue": out, "total": len(out),
                "running": running[0].get("title") if running else "",
                "cooldown": max(0, int(_cooldown_until - time.time()))}


def move(rid, direction) -> dict:
    """调整队列顺序：up / down / top / bottom。"""
    direction = str(direction or "").lower()
    if direction not in ("up", "down", "top", "bottom"):
        return {"ok": False, "msg": "未知的移动方向"}
    with _lock:
        _queue_alive()
        q = _db["queue"]
        if rid not in q:
            return {"ok": False, "msg": "这单不在队列里（可能已经在制作或已结束）"}
        i = q.index(rid)
        if direction == "top":
            j = 0
        elif direction == "bottom":
            j = len(q) - 1
        elif direction == "up":
            j = max(0, i - 1)
        else:
            j = min(len(q) - 1, i + 1)
        if j != i:
            q.pop(i)
            q.insert(j, rid)
            _save()
        return {"ok": True, "msg": "已移到第 %d 位" % (j + 1), "pos": j + 1, "total": len(q)}


def _launch_locked(r) -> dict:
    """在持锁状态下启动一单（必须已经不在队列里）。返回 {ok,msg,watch,reuse}。"""
    if r.get("type") == TYPE_TTS:
        if not tts_mod:
            return {"ok": False, "msg": "文字转语音功能未启用"}
        res = tts_mod.start({"text": r.get("text", ""), "voice": r.get("voice", ""),
                             "rate": r.get("rate", "正常"), "base": r.get("base") or ""})
        status_fn = tts_mod.job_status
    else:
        model = _valid_model(r.get("model"))
        if not model:
            return {"ok": False, "msg": "这条用的声线模型已经不在硬盘上了，只能拒绝"}
        r["model"] = model
        res = cover_mod.start_cover({"song_id": r.get("song_id"), "title": r.get("title"),
                                     "artist": r.get("artist"), "model": model,
                                     "key_shift": 0, "acapella": bool(r.get("acapella"))})
        status_fn = cover_mod.job_status

    if not res.get("ok"):
        return {"ok": False, "msg": "启动失败：%s" % res.get("msg")}
    r["job"] = res.get("job")
    r["error"] = None
    r["decided"] = time.time()
    if res.get("cached"):
        # 成品库已有同参数的成品：任务一创建就是 done，不必真等制作
        st = status_fn(res.get("job")) or {}
        name = st.get("file") or res.get("file")
        if name:
            r.update(state=STATE_DONE, file=name, reuse=True,
                     size_text=st.get("size_text", ""))
            return {"ok": True, "reuse": True, "msg": "已有成品，直接发给对方（没再跑一遍）"}
    r["state"] = STATE_RUNNING
    # 挂到正在跑的同参数任务上，也算复用（同样不重复占 GPU）
    r["reuse"] = bool(res.get("shared"))
    return {"ok": True, "watch": (r["id"], res.get("job"), status_fn)}


def pump(force=False) -> dict:
    """推进队列：没人在做就取队首开跑。返回 {ok, started, msg, watch}。"""
    global _cooldown_until
    with _lock:
        _prune_zombies()      # 面板重启后残留的 running 先清掉，否则队列永远推不动
        _queue_alive()
        running = [x for x in _db["requests"] if x.get("state") == STATE_RUNNING]
        if running:
            return {"ok": True, "started": None, "msg": "有一单在做，队列继续等"}
        if not _db["queue"]:
            _save()
            return {"ok": True, "started": None, "msg": "队列是空的"}
        if not force and time.time() < _cooldown_until:
            return {"ok": False, "started": None,
                    "msg": "刚失败过，%d 秒后再自动往下推（也可以点「立即开始下一单」）"
                           % max(0, int(_cooldown_until - time.time()))}
        # 一次推进一单；队首无效（被删/取消）就跳过继续看下一个
        for _ in range(len(_db["queue"]) + 1):
            if not _db["queue"]:
                break
            rid = _db["queue"].pop(0)
            r = _find(rid)
            if not r or r.get("state") != STATE_QUEUED:
                continue
            res = _launch_locked(r)
            if not res.get("ok"):
                r.update(state=STATE_ERROR, error=str(res.get("msg"))[:200])
                _cooldown_until = time.time() + FAIL_COOLDOWN
                _save()
                return {"ok": False, "started": rid, "msg": res.get("msg")}
            _save()
            return {"ok": True, "started": rid, "msg": res.get("msg") or "开始制作",
                    "watch": res.get("watch"), "reuse": bool(res.get("reuse"))}
        _save()
        return {"ok": True, "started": None, "msg": "队列里没有可做的单"}


def _pump_async(force=False):
    """在独立线程里推队列（避免持锁时递归/阻塞请求）。"""
    def run():
        try:
            res = pump(force=force)
            w = res.get("watch")
            if w:
                threading.Thread(target=_watch, args=w, daemon=True).start()
        except Exception:
            traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()


def pump_now(force=True) -> dict:
    """同步推一次队列并接上监视线程，返回值可以直接丢给前端。"""
    res = pump(force=force)
    w = res.pop("watch", None)
    if w:
        threading.Thread(target=_watch, args=w, daemon=True).start()
    if res.get("started"):
        r = _find(res["started"]) or {}
        res["state"] = r.get("state")
    return res


def _cache_ready(r) -> bool:
    """这单是不是成品库里已经有现成的（命中就不用占 GPU，也不必排队）。"""
    try:
        if r.get("type") == TYPE_TTS:
            if not tts_mod:
                return False
            v = tts_mod.validate({"text": r.get("text", ""), "voice": r.get("voice", ""),
                                  "rate": r.get("rate", "正常"), "base": r.get("base") or ""})
            if not v.get("ok"):
                return False
            return bool(tts_mod._find_cached(
                tts_mod.text_key(v["text"], v["voice"], v["base"], v["rate"])))
        model = _valid_model(r.get("model"))
        if not model:
            return False
        return bool(cover_mod.find_cached(r.get("song_id"), model, 0, bool(r.get("acapella"))))
    except Exception:
        return False


def approve(rid):
    """批准 = 进入队列（不再要求前一单做完）。空闲时立即开跑。"""
    with _lock:
        r = _find(rid)
        if not r:
            return {"ok": False, "msg": "申请不存在"}
        if r.get("state") not in (STATE_PENDING, STATE_REJECTED, STATE_ERROR):
            return {"ok": False, "msg": "这条已处理过（当前状态：%s）" % r.get("state")}
        r.update(state=STATE_QUEUED, decided=time.time(), error=None, job=None)
        r.pop("reuse", None)
        # 命中成品库的单不需要 GPU：直接发放，别排在队尾干等
        if _cache_ready(r):
            res = _launch_locked(r)
            if res.get("ok") and r.get("state") == STATE_DONE:
                _save()
                return {"ok": True, "reuse": True, "file": r.get("file"),
                        "msg": "已有成品，直接发给对方（没再跑一遍，也不占队列）"}
            r.update(state=STATE_QUEUED, job=None)      # 没真命中就老实排队
        ahead = _enqueue(rid)
        _save()
    # 空闲就立刻开跑；忙就安静排队
    pump_now(force=True)
    r = _find(rid) or {}
    if r.get("state") == STATE_DONE:
        return {"ok": True, "reuse": True, "file": r.get("file"),
                "msg": "已有成品，直接发给对方（没再跑一遍）"}
    if r.get("state") == STATE_RUNNING:
        return {"ok": True, "msg": "已批准，马上开始（队列已空）", "job": r.get("job")}
    if r.get("state") == STATE_ERROR:
        return {"ok": True, "msg": "已批准但启动失败：%s" % (r.get("error") or "")}
    # 真正的排队位次（可能因为前面有人插队而变）
    q = _db.get("queue") or []
    pos = (q.index(rid) + 1) if rid in q else max(1, ahead + 1)
    return {"ok": True, "pos": pos, "total": len(q),
            "msg": "已批准，排在第 %d 位" % pos}


def reject(rid, note=""):
    with _lock:
        r = _find(rid)
        if not r:
            return {"ok": False, "msg": "申请不存在"}
        if r.get("state") == STATE_DONE:
            return {"ok": False, "msg": "已经做完了，不能拒绝"}
        if r.get("state") == STATE_RUNNING:
            return {"ok": False, "msg": "正在制作，停不下来；做完再处理"}
        was_queued = r.get("state") == STATE_QUEUED
        r["state"] = STATE_REJECTED
        r["decided"] = time.time()
        r["note"] = str(note or "")[:120]
        _queue_alive()
        _save()
    if was_queued:
        _pump_async()
    return {"ok": True, "msg": "已移出队列并拒绝" if was_queued else "已拒绝"}


WATCH_TIMEOUT = 45 * 60      # 单任务最多盯 45 分钟；再久就当它卡死，放行队列


def _watch(rid, jid, status_fn=None):
    """盯住任务（翻唱或朗读），完成后回写成品文件名，再接着推队列。"""
    status_fn = status_fn or cover_mod.job_status
    global _cooldown_until
    t0 = time.time()
    while True:
        time.sleep(3)
        # 兜底：后端卡死（gradio_client 某些情况下没有读超时）会让这单永远 running，
        # 队列就再也推不动。到点强制转 error，让后面的单能继续。
        if time.time() - t0 > WATCH_TIMEOUT:
            with _lock:
                r = _find(rid)
                if r and r.get("state") == STATE_RUNNING:
                    r.update(state=STATE_ERROR,
                             error="超过 %d 分钟没出结果，已判定卡死并放行队列；"
                                   "请到后端日志看它是不是还挂在推理里"
                                   % (WATCH_TIMEOUT // 60))
                    _save()
            _cooldown_until = time.time() + FAIL_COOLDOWN
            _pump_async()
            return
        st = status_fn(jid)
        if not st:
            with _lock:
                r = _find(rid)
                if r and r.get("state") == STATE_RUNNING:
                    r.update(state=STATE_ERROR, error="任务记录丢失（面板可能重启过）")
                    _save()
            _pump_async()
            return
        with _lock:
            r = _find(rid)
            if not r:
                return
            if st.get("state") == "done":
                r.update(state=STATE_DONE, file=st.get("file"),
                         size_text=st.get("size_text", ""),
                         decided=st.get("ended") or time.time())
                _save()
                _pump_async()
                return
            if st.get("state") == "error":
                r.update(state=STATE_ERROR, error=str(st.get("error"))[:200])
                _cooldown_until = time.time() + FAIL_COOLDOWN
                _save()
                _pump_async()
                return


def file_for(gid, rid, is_admin=False, alt_gid=""):
    """取访客自己的成品文件；管理员不限。

    alt_gid 用于「Cookie 丢了、靠 localStorage 里的访客 id 找回」的场景，
    否则找回后能看见列表却下不了文件。两个 id 都是 64 位随机值，等价于凭据。
    """
    with _lock:
        r = _find(rid)
        if not r:
            return None
        if not is_admin:
            owners = {x for x in (gid, str(alt_gid or "").strip()) if x}
            if r.get("gid") not in owners:
                return None
        if r.get("state") != "done" or not r.get("file"):
            return None
        name = r["file"]
    return cover_mod.file_abs(name)


# ---------------- 自动巡队 ----------------
def _ticker():
    """兜底：队列里还有人、又没人在跑，就推一把。

    面板重启后面排队中的单没有 job 记录（不会自己恢复），靠这只看门狗接着做；
    也防「监视线程意外死掉」导致队列卡住。测试实例用 RVC_NO_AUTOPUMP=1 关掉。
    """
    time.sleep(6)                      # 让后端先起来，别一上来就把队列打成 error
    while True:
        try:
            flush()                    # 补上被节流的访客/搜索计数
            if _db.get("queue"):
                _pump_async()
        except Exception:
            traceback.print_exc()
        time.sleep(PUMP_GAP if _db.get("queue") else 15)


if not os.environ.get("RVC_NO_AUTOPUMP"):
    threading.Thread(target=_ticker, daemon=True).start()


# ---------------- 页面 ----------------
_CACHE = {}


def _read(name):
    if name not in _CACHE:
        with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
            _CACHE[name] = f.read()
    return _CACHE[name]


def page():
    return _read("guest_page.html")


# ---------------- 路由 ----------------
def handle_get(path, query, gid, is_admin=False, ip=""):
    q = query or {}
    # 公网侧先过一道总闸：扫描器会把 /api/guest/* 挨个试一遍
    if not is_admin and path.startswith("/api/guest/") and not _api_allowed(ip):
        return 429, {"ok": False, "msg": "请求太频繁了，稍等一下再试"}
    try:
        if path == "/api/guest/models":
            res = cover_mod.models()
            # 不把服务器绝对路径吐给公网访客
            res.pop("dir", None)
            return 200, res
        if path == "/api/guest/captcha":
            return 200, captcha_mod.new(gid, ip)
        if path == "/api/guest/search":
            kw = (q.get("kw") or [""])[0].strip()[:MAX_KW]
            if not kw:
                return 200, {"ok": False, "msg": "请输入歌名"}      # 没填关键词不消耗验证码

            okf, why = _search_allowed(ip)
            if not okf:
                return 429, {"ok": False, "msg": why}

            info = {}
            cp = (q.get("cp") or [""])[0]
            if cp:
                # 通行证：10 分钟内同一访客同一 IP 连续搜索，不用反复看图
                ok, msg, left = captcha_mod.check_pass(cp, gid, ip)
                if not ok:
                    return 400, {"ok": False, "code": "captcha", "msg": msg, "need_new": True}
                info = {"pass_left": left}
            else:
                ok, msg, info = captcha_mod.consume((q.get("ct") or [""])[0],
                                                    (q.get("ca") or [""])[0], gid, ip)
                if not ok:
                    payload = {"ok": False, "code": "captcha", "msg": msg}
                    payload.update(info or {})
                    return 400, payload
            res = cover_mod.search(kw, 12)
            if res.get("ok"):
                touch_search(gid)
            if info.get("pass"):
                res.update({"pass": info["pass"], "pass_ttl": info.get("pass_ttl"),
                            "pass_left": info.get("pass_left")})
            elif "pass_left" in info:
                res["pass_left"] = info["pass_left"]
            return 200, res
        if path == "/api/guest/voices":
            if not tts_mod:
                return 200, {"ok": False, "msg": "文字转语音功能未启用"}
            return 200, tts_mod.voices()
        if path == "/api/guest/my":
            alt = ((q.get("gid") or [""])[0] if isinstance(q.get("gid"), list)
                   else (q.get("gid") or ""))
            return 200, my_requests(gid, ip, alt)
        if path == "/api/guest/exists":
            # 这首歌已经做过哪些组合（访客选完歌就能看到，避免白等一次制作）
            sid = (q.get("song_id") or [""])[0] if isinstance(q.get("song_id"), list) else (q.get("song_id") or "")
            variants = cover_mod.cached_variants(sid)
            model = os.path.basename(str(
                (q.get("model") or [""])[0] if isinstance(q.get("model"), list) else (q.get("model") or "")))
            acapella = str((q.get("acapella") or ["0"])[0] if isinstance(q.get("acapella"), list)
                           else (q.get("acapella") or "0")).lower() in ("1", "true", "yes")
            exact = bool(model) and any(
                v["model"].lower() == model.lower() and
                int(v["key_shift"] or 0) == 0 and
                bool(v["acapella"]) == acapella for v in variants)
            return 200, {"ok": True, "variants": variants, "exact": exact,
                         "labels": [v["text"] for v in variants]}
        if path == "/api/guest/wl":
            # 白名单状态：fp 由前端带上来（服务端再配 IP），匹配逻辑全在服务端
            fp = (q.get("fp") or [""])[0] if isinstance(q.get("fp"), list) else (q.get("fp") or "")
            return 200, wl_mine(gid, fp, ip)
        if path == "/api/admin/visits":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, visits()
        if path == "/api/admin/requests":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            st = (q.get("state") or ["all"])[0]
            return 200, admin_requests(st)
        if path == "/api/admin/wl":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, wl_admin_view()
        if path == "/api/admin/queue":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, queue_list()
    except Exception as e:
        traceback.print_exc()
        # 详情只留给本机：给公网访客返回异常类名/路径等于免费送内部结构
        if is_admin:
            return 500, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
        return 500, {"ok": False, "msg": "服务内部错误，请稍后再试"}
    return 404, {"ok": False, "msg": "not found"}


def handle_post(path, body, gid, ip, country, ua, is_admin=False):
    body = body or {}
    try:
        if path == "/api/guest/request":
            return 200, create_request(gid, ip, country, ua, body)
        if path == "/api/guest/wl_apply":
            return 200, wl_apply(gid, str(body.get("fp") or ""), ip, country, ua,
                                 body.get("reason"))
        if path == "/api/admin/wl_decide":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, wl_decide(str(body.get("id") or ""),
                                  str(body.get("action") or ""), body.get("note"))
        if path == "/api/admin/wl_revoke":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, wl_revoke(str(body.get("gid") or ""))
        if path == "/api/admin/approve":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, approve(str(body.get("id") or ""))
        if path == "/api/admin/reject":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, reject(str(body.get("id") or ""), body.get("note"))
        if path == "/api/admin/move":
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, move(str(body.get("id") or ""), body.get("dir") or body.get("direction"))
        if path in ("/api/admin/pump", "/api/admin/next"):
            if not is_admin:
                return 403, {"ok": False, "msg": "需要管理员"}
            return 200, pump_now(force=True)
    except Exception as e:
        traceback.print_exc()
        # 详情只留给本机：给公网访客返回异常类名/路径等于免费送内部结构
        if is_admin:
            return 500, {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
        return 500, {"ok": False, "msg": "服务内部错误，请稍后再试"}
    return 404, {"ok": False, "msg": "not found"}
