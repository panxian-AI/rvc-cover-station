# -*- coding: utf-8 -*-
"""访客搜索用的图像验证码（v2：一次验证、十分钟内连续搜索免重复）。

一次完整流程：
    GET  /api/guest/captcha            -> {token, img, ttl}
    GET  /api/guest/search?ct=&ca=     -> 校验通过后返回 {pass, pass_ttl, pass_left, songs...}
    GET  /api/guest/search?cp=<pass>   -> 直接放行，不再要图（配额内）

设计要点：
    1. 图像验证码仍然是「机器难、人容易」：真 PNG 位图（无文字层），字符旋转 + 干扰线 + 噪点。
       相比 v1 提高了可读性（更大的字、更少的噪点、旋转收窄），减少正常访客反复输错。
    2. 一张图可试 3 次：输错不再立刻作废，只递减次数，避免打错一个字就重看图。
    3. 通过后发一张「通行证」（pass），默认 10 分钟 / 12 次搜索。同一访客连续搜几首歌不用反复看图，
       但仍受总次数约束 —— 脚本想批量刷接口，还是得每次重新过图。
    4. token 绑访客 gid，3 分钟过期；pass 绑 gid + IP，换设备/换网络立刻失效。
    5. 猜错次数按 IP 阶梯锁定：连错 6 次锁 1 分钟，再错锁 3 分钟，之后 10 分钟。
    6. 取图限速 40 张 / 10 分钟 / IP，防止有人狂刷新图把 CPU 打满。
"""
import base64
import io
import math
import os
import random
import secrets
import threading
import time

# 去掉容易看错的 0 O 1 I l S 5 B 8 Z 2
ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"
LEN = 4
TTL = 180                 # 一张图 3 分钟
MAX_TOKENS = 600
MAX_TRY = 3               # 同一张图允许输错几次
PASS_TTL = 600            # 通行证 10 分钟
PASS_QUOTA = 12           # 通行证可用几次搜索
MAX_PASSES = 400
LOCK_STEPS = (6, 9, 12)   # 连错到第 6/9/12 次分别锁
LOCK_SECS = (60, 180, 600)
GEN_LIMIT = 40            # 同一 IP 10 分钟内最多取多少张图（防刷图耗 CPU）
GEN_WINDOW = 600
GEN_GLOBAL_LIMIT = 150    # 全局 10 分钟取图总闸：换一堆 IP 也刷不动，护住 CPU
ULREQ_WINDOW = 600        # 解锁请求限频：同一 IP 10 分钟只能发一次（不管批没批）
ULREQ_TTL = 3600          # 解锁请求记录保留 1 小时（访客轮询结果用）
ULREQ_MAX = 50            # 待处理请求上限，防队列被灌爆

_lock = threading.RLock()
_tokens = {}              # token -> {"code", "gid", "exp", "tries"}
_passes = {}              # pass  -> {"gid", "ip", "exp", "left"}
_fails = {}               # ip -> {"streak": int, "until": float}
_gen = {}                 # ip -> [时间戳]
_gen_all = []             # 全局取图时间戳（不分 IP）
_ulreqs = {}              # ip -> {"t", "status": pending/ok/no, "streak"} 解锁请求

_WARNED = False


# ---------------- 字体 ----------------
_FONTS = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\consolab.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font_path():
    for p in _FONTS:
        if os.path.isfile(p):
            return p
    return None


def _import_pil():
    """返回 PIL 模块组；必要时把 apienv 的 site-packages 挂进来再试。"""
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return Image, ImageDraw, ImageFont
    except ImportError:
        pass
    try:
        import glob
        import site
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [os.path.join(os.path.dirname(here), "apienv", "Lib", "site-packages")]
        try:
            from paths import PY_API
            cands.append(os.path.join(os.path.dirname(PY_API), "..", "Lib", "site-packages"))
        except Exception:
            pass
        cands += glob.glob(os.path.join(os.path.dirname(here), "*", "Lib", "site-packages"))
        for c in cands:
            c = os.path.abspath(c)
            if os.path.isdir(c) and c not in sys.path:
                site.addsitedir(c)
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except Exception:
        return None


# ---------------- 画图 ----------------
def _draw_png(code):
    mods = _import_pil()
    if not mods:
        return None
    Image, ImageDraw, ImageFont = mods

    S = 2                                   # 先按 2 倍画再缩小，边缘更顺
    W, H = 160 * S, 56 * S
    base = random.randint(234, 246)
    img = Image.new("RGB", (W, H), (base, base, base - 4))
    d = ImageDraw.Draw(img)

    # 斜向淡色条纹（更淡，保证字看得清）
    for _ in range(5):
        x0, y0 = random.randint(-W, W), random.randint(-H, H)
        col = tuple(random.randint(196, 226) for _ in range(3))
        d.line([(x0, y0),
                (x0 + random.randint(120, 320) * S, y0 + random.randint(-70, 70) * S)],
               fill=col, width=1 * S)

    # 噪点（数量减半，位置避开字心区）
    for _ in range(110):
        g = random.randint(140, 215)
        d.point((random.randint(0, W - 1), random.randint(0, H - 1)), fill=(g, g, g))

    # 逐字画，各自轻微旋转、抖动
    fp = _font_path()
    step = (W - 26 * S) / float(len(code))
    for i, ch in enumerate(code):
        size = random.randint(30, 36) * S
        try:
            font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        cell = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        cd = ImageDraw.Draw(cell)
        col = (random.randint(12, 42), random.randint(20, 54), random.randint(64, 108))
        cd.text((size // 2, size // 2), ch, font=font, fill=col + (255,))
        cell = cell.rotate(random.uniform(-18, 18), resample=Image.BICUBIC, expand=False)
        x = int(12 * S + i * step + random.uniform(-2, 2) * S)
        y = int((H - cell.size[1]) / 2 + random.uniform(-4, 4) * S)
        img.paste(cell, (x, y), cell)

    # 一条干扰曲线（压在字上方，但不遮主干）
    amp = random.randint(7, 12) * S
    phase = random.random() * 6
    pts = [(x, int(H / 2 + math.sin(x / (14.0 * S) + phase) * amp))
           for x in range(0, W + 6, 6)]
    d.line(pts, fill=tuple(random.randint(120, 176) for _ in range(3)), width=S, joint="curve")

    img = img.resize((W // S, H // S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _draw_svg(code):
    """兜底备用（当前 **不再使用**）。

    留着只作参考：它把字符当 `<text>` 写进 SVG，答案等于明文放在源码里，
    任何脚本读一下 XML 就能过验证码 —— 那不是「弱一点」，是等于没有验证码。
    所以 new() 在拿不到真位图时直接拒绝出图，宁可暂停搜索也不放行。
    """
    W, H = 160, 56
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">' % (W, H),
             '<rect width="100%%" height="100%%" fill="#eef2f8"/>']
    for _ in range(6):
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#c9d4e3"/>'
                     % (random.randint(-40, W), random.randint(0, H),
                        random.randint(0, W + 40), random.randint(0, H)))
    for i, ch in enumerate(code):
        x = 20 + i * 36 + random.randint(-3, 3)
        y = 40 + random.randint(-4, 4)
        rot = random.uniform(-18, 18)
        parts.append('<text x="%d" y="%d" font-family="Arial" font-size="%d" font-weight="700" '
                     'fill="rgb(%d,%d,%d)" text-anchor="middle" '
                     'transform="rotate(%.1f %d %d)">%s</text>'
                     % (x, y, random.randint(30, 36), random.randint(14, 50),
                        random.randint(24, 64), random.randint(64, 112), rot, x, y, ch))
    parts.append('<path d="M0 %d Q 40 %d 80 %d T 160 %d" fill="none" stroke="#8fa4bd"/>'
                 % (random.randint(10, 44), random.randint(6, 50),
                    random.randint(10, 44), random.randint(10, 44)))
    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


# ---------------- 状态 ----------------
def _prune(now):
    if len(_tokens) <= MAX_TOKENS:
        return
    for k in [k for k, v in _tokens.items() if v["exp"] < now]:
        _tokens.pop(k, None)
    if len(_tokens) > MAX_TOKENS:                       # 还超就砍最早的一半
        for k in sorted(_tokens, key=lambda k: _tokens[k]["exp"])[:len(_tokens) // 2]:
            _tokens.pop(k, None)


def _prune_passes(now):
    dead = [k for k, v in _passes.items() if v["exp"] < now or v["left"] <= 0]
    for k in dead:
        _passes.pop(k, None)
    if len(_passes) > MAX_PASSES:
        for k in sorted(_passes, key=lambda k: _passes[k]["exp"])[:len(_passes) // 2]:
            _passes.pop(k, None)


def _gen_allowed(ip):
    """取图频率限制，防止有人狂刷新图（每张都要真渲染，很吃 CPU）。

    两道闸：单 IP 40 张 / 10 分钟，全局 150 张 / 10 分钟。
    后一道是给「换一堆代理 IP 分布式刷」准备的 —— 单 IP 限流挡不住那种。
    """
    now = time.time()
    with _lock:
        global _gen_all
        _gen_all = [t for t in _gen_all if now - t < GEN_WINDOW]
        if len(_gen_all) >= GEN_GLOBAL_LIMIT:
            return False                     # 全局吃紧，先不给新图（正常用户几分钟后就恢复）
        if not ip:
            _gen_all.append(now)
            return True
        hits = [t for t in _gen.get(ip, []) if now - t < GEN_WINDOW]
        if len(hits) >= GEN_LIMIT:
            _gen[ip] = hits
            return False
        hits.append(now)
        _gen[ip] = hits
        _gen_all.append(now)
        if len(_gen) > 2000:
            for k in list(_gen)[:1000]:
                _gen.pop(k, None)
        return True


def new(gid="", ip=""):
    """生成一张新图。返回 {ok, token, img(data URI), ttl, tries}。"""
    if not _gen_allowed(ip):
        return {"ok": False, "msg": "验证码刷新太频繁，请稍等一会", "ttl": 0}
    # 字符用 secrets 取，不用 random：random 是 Mersenne Twister，状态可从输出反推。
    code = "".join(secrets.choice(ALPHABET) for _ in range(LEN))
    png = _draw_png(code)
    if not png:
        # 没有 Pillow 就没有真位图。**绝不退回 SVG** —— 那样答案以明文写在图片源码里，
        # 等于把验证码关掉。宁可暂停搜索，让站长看到提示去换 apienv 的解释器。
        return {"ok": False, "ttl": 0,
                "msg": "验证码渲染组件不可用（缺 Pillow），已暂停搜索。"
                       "请用 apienv 里的 Python 启动面板。"}

    token = secrets.token_urlsafe(12)
    now = time.time()
    with _lock:
        _tokens[token] = {"code": code, "gid": gid or "", "exp": now + TTL, "tries": MAX_TRY}
        _prune(now)
    return {"ok": True, "token": token, "ttl": TTL, "tries": MAX_TRY,
            "img": "data:image/png;base64,%s" % base64.b64encode(png).decode("ascii")}


def _wait_secs(ip):
    f = _fails.get(ip or "")
    if not f:
        return 0
    left = f.get("until", 0) - time.time()
    return int(left) + 1 if left > 0 else 0


def _fail(ip):
    """记一次输错；连错到阶梯点（及以上）就锁定。

    必须用 `>=` 且记住「已经用到第几档」：以前是 `streak == 6/9/12`，
    streak 一旦涨到 13，三个等值判断全不成立 —— 之后的每一次输错都不再锁，
    爆破只需要扛过前 12 次。
    """
    ip = ip or ""
    with _lock:
        f = _fails.setdefault(ip, {"streak": 0, "until": 0, "tier": -1})
        f["streak"] = int(f.get("streak", 0)) + 1
        tier = -1
        for i, step in enumerate(LOCK_STEPS):
            if f["streak"] >= step:
                tier = i
        # 只在跨入更高一档时续锁：同一档内继续错保持现有 until，别无限叠加
        if tier > int(f.get("tier", -1)):
            f["tier"] = tier
            f["until"] = time.time() + LOCK_SECS[tier]
        elif tier >= 0 and f["streak"] > LOCK_STEPS[tier]:
            # 已过最高档仍在错：按最高档时长继续锁（冷却结束又错一次就再锁）
            if f["until"] <= time.time():
                f["until"] = time.time() + LOCK_SECS[tier]
        if len(_fails) > 2000:
            for k in list(_fails)[:1000]:
                _fails.pop(k, None)


def _ok(ip):
    with _lock:
        f = _fails.get(ip or "")
        if f:
            f["streak"] = 0
            f["until"] = 0
            f["tier"] = -1


def consume(token, answer, gid="", ip=""):
    """校验验证码。返回 (ok, msg, info)。

    info 里可能带：
        tries_left  这张图还能再试几次（token 仍有效）
        need_new    这张图已作废，必须换一张
        pass/pass_ttl/pass_left  通过后拿到的通行证
    """
    wait = _wait_secs(ip)
    if wait:
        return False, ("验证码错误次数太多，已临时锁定，还剩 %d 秒。"
                       "可以先向站长请求解锁（只解这一次），或等锁定结束再试") % wait, \
            {"need_new": False, "locked": True, "left": wait}

    token = (token or "").strip()
    answer = (answer or "").strip().replace(" ", "").upper()
    now = time.time()
    with _lock:
        item = _tokens.get(token)
    if not item:
        return False, "验证码已失效，请换一张", {"need_new": True}
    if item["exp"] < now:
        with _lock:
            _tokens.pop(token, None)
        return False, "验证码已过期，请换一张", {"need_new": True}
    if gid and item["gid"] and item["gid"] != gid:
        with _lock:
            _tokens.pop(token, None)
        return False, "验证码和你的会话不匹配，请刷新页面重试", {"need_new": True}
    if not answer:
        return False, "请输入图中的字符", {"tries_left": int(item.get("tries", 0))}
    if answer != item["code"]:
        _fail(ip)
        with _lock:
            item["tries"] = int(item.get("tries", 1)) - 1
            left = item["tries"]
            if left <= 0:
                _tokens.pop(token, None)
        if left <= 0:
            return False, "验证码不对，这张已作废，请换一张", {"need_new": True}
        return False, "验证码不对，还可以再试 %d 次" % left, {"tries_left": left}

    # 通过：销毁这张图，发通行证
    _ok(ip)
    pt = secrets.token_urlsafe(18)
    with _lock:
        _tokens.pop(token, None)
        _passes[pt] = {"gid": gid or "", "ip": ip or "",
                       "exp": now + PASS_TTL, "left": PASS_QUOTA}
        _prune_passes(now)
    return True, "", {"pass": pt, "pass_ttl": PASS_TTL, "pass_left": PASS_QUOTA}


def check_pass(pass_token, gid="", ip=""):
    """用通行证免验证码搜索。返回 (ok, msg, 剩余次数)。每次成功扣 1 次配额。"""
    pt = (pass_token or "").strip()
    if not pt:
        return False, "需要验证码", 0
    now = time.time()
    with _lock:
        item = _passes.get(pt)
        if not item:
            return False, "验证已过期，请重新输入验证码", 0
        if item["exp"] < now:
            _passes.pop(pt, None)
            return False, "验证已过期，请重新输入验证码", 0
        if item["gid"] and gid and item["gid"] != gid:
            _passes.pop(pt, None)
            return False, "验证已失效，请重新输入验证码", 0
        if item["ip"] and ip and item["ip"] != ip:
            _passes.pop(pt, None)
            return False, "验证已失效，请重新输入验证码", 0
        if item["left"] <= 0:
            _passes.pop(pt, None)
            return False, "本次验证的搜索次数用完了，请重新输入验证码", 0
        item["left"] -= 1
        left = item["left"]
    return True, "", left


def revoke_pass(pass_token):
    """主动作废（换一张验证码时用）。"""
    with _lock:
        _passes.pop((pass_token or "").strip(), None)


def stats():
    now = time.time()
    with _lock:
        return {"tokens": len(_tokens),
                "passes": len(_passes),
                "locked_ips": sum(1 for v in _fails.values() if v.get("until", 0) > now)}


# ---------------- 锁定信息与「请求站长解锁」（v1.3.0） ----------------
def lock_info(ip):
    """当前 IP 的锁定状态：是否被锁、还剩几秒、已连错几次。"""
    ip = ip or ""
    with _lock:
        f = _fails.get(ip) or {}
        left = int(max(0, (f.get("until") or 0) - time.time()))
        return {"locked": left > 0, "left": left,
                "streak": int(f.get("streak", 0) or 0)}


def _prune_ulreqs(now):
    dead = [k for k, v in _ulreqs.items() if now - v.get("t", 0) > ULREQ_TTL]
    for k in dead:
        _ulreqs.pop(k, None)
    if len(_ulreqs) > ULREQ_MAX:
        for k in sorted(_ulreqs, key=lambda k: _ulreqs[k].get("t", 0))[:len(_ulreqs) - ULREQ_MAX]:
            _ulreqs.pop(k, None)


def request_unlock(ip):
    """访客向站长发一条解锁请求。返回 (ok, msg)。

    限频：同一 IP 10 分钟只能发一次（无论上次批没批）；没被锁不给发。
    """
    ip = ip or ""
    now = time.time()
    with _lock:
        _prune_ulreqs(now)
        info = lock_info(ip)
        if not info["locked"]:
            return False, "你现在没有被锁定，直接输入验证码就行，不用申请解锁"
        old = _ulreqs.get(ip)
        if old and now - old["t"] < ULREQ_WINDOW:
            left = int(ULREQ_WINDOW - (now - old["t"]))
            tag = {"pending": "还在等站长处理", "ok": "上次已被同意过",
                   "no": "上次被站长拒绝"}.get(old.get("status"), "刚发过请求")
            return False, "%s，%d 秒内不能再发（请别重复刷）" % (tag, left)
        _ulreqs[ip] = {"t": now, "status": "pending", "streak": info["streak"]}
        _prune_ulreqs(now)
    return True, ("请求已发给站长，页面会自动等待结果。注意：解锁只解除这一次"
                  "锁定，之后再输错还是会照常锁定")


def unlock_status(ip):
    """访客轮询：我的解锁请求现在什么状态。"""
    ip = ip or ""
    now = time.time()
    with _lock:
        _prune_ulreqs(now)
        req = _ulreqs.get(ip)
        info = lock_info(ip)
        out = {"locked": info["locked"], "left": info["left"], "streak": info["streak"]}
        if req:
            out["status"] = req["status"]
            out["age"] = int(now - req["t"])
        return out


def pending_unlocks():
    """站长端：待处理的解锁请求（新到旧）。"""
    now = time.time()
    with _lock:
        _prune_ulreqs(now)
        rows = [{"ip": ip, "t": v["t"], "age": int(now - v["t"]),
                 "streak": v.get("streak", 0)}
                for ip, v in _ulreqs.items() if v.get("status") == "pending"]
        rows.sort(key=lambda r: -r["t"])
        return {"ok": True, "pending": rows, "count": len(rows)}


def decide_unlock(ip, allow):
    """站长审批一条解锁请求。返回 (ok, msg)。

    **一次性**：同意只清除「当前正在生效的这次锁定」，连错计数（streak）原样保留 ——
    之后只要再输错，还是按原来的阶梯继续锁，绝不会因为批过一次就从此豁免。
    没有正在生效的锁定时也允许「同意」（比如刚自己等到过期），但同样不会豁免后续。
    """
    ip = (ip or "").strip()
    if not ip:
        return False, "缺少 IP"
    with _lock:
        req = _ulreqs.get(ip)
        if not req:
            return False, "这个 IP 没有待处理的解锁请求"
        if req.get("status") != "pending":
            return False, "这条请求已经处理过了"
        req["status"] = "ok" if allow else "no"
        cleared = False
        f = _fails.get(ip)
        if allow and f and (f.get("until") or 0) > time.time():
            f["until"] = 0          # 只解除当前这一次；streak 保留，不是豁免
            cleared = True
    try:
        from panel import sec_log  # 面板安全日志（同进程，直接调）；单独导入 captcha 时不强求
        sec_log("验证码解锁请求已%s ip=%s（一次性，非豁免）" % ("同意" if allow else "拒绝", ip))
    except Exception:
        pass
    if allow:
        return True, ("已同意 %s：当前锁定已解除（这一次有效）" % ip) if cleared \
            else ("已同意 %s：它当前没有生效中的锁定" % ip)
    return True, "已拒绝 %s 的解锁请求" % ip
