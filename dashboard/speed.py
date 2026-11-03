# -*- coding: utf-8 -*-
"""多线路测速：给下载任务挑最快的那条线。

为什么单独一个模块：
    更新包和模型文件都有多条下载线路（GitHub 直连 + 若干加速镜像、HuggingFace
    官方 + 国内镜像）。写死顺序的话，遇到某条线抽风就只能干等超时；这里先并行
    探一小段真实数据，按实测速度排序，再按顺序下载，失败自动换下一条。

设计要点：
    · probe() 用 Range 请求只取前 N 字节，按「字节数 / 耗时」算 KB/s，不动整文件；
    · 直连优先，直连失败且系统配了代理时再走代理（避免面板继承到奇怪的代理变量）；
    · 证书校验失败时降级重试一次（个别国内镜像证书链不全），并记在结果里；
    · rank() 并行探测（默认 4 路），失败线路排在后面但仍保留 —— 探测失败不等于下载
      一定失败（有的镜像不支持 Range），所以只做「排序」不做「淘汰」；
    · 全部失败时按原顺序返回，让调用方自己重试。
"""
from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rvc-cover-station/1.1"

DEFAULT_BYTES = 262144          # 探 256KB，够看出速度差异又不浪费流量
DEFAULT_TIMEOUT = 8


def _ctx(insecure: bool):
    if not insecure:
        return ssl.create_default_context()
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _env_proxy() -> str:
    """系统里配置的 HTTP(S) 代理（没有就空串）。"""
    import os
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""


def _open(url: str, timeout: float, headers: dict | None, proxy: str, insecure: bool):
    handlers = []
    # proxy="" 表示不用代理（ProxyHandler({})），proxy=具体地址则显式指过去
    handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    handlers.append(urllib.request.HTTPSHandler(context=_ctx(insecure)))
    op = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    return op, req


def probe(url: str, nbytes: int = DEFAULT_BYTES, timeout: float = DEFAULT_TIMEOUT,
          headers: dict | None = None) -> dict:
    """取一小段看速度。返回 {ok, ms, kbps, bytes, code, error}，永不抛异常。"""
    hdrs = dict(headers or {})
    hdrs.setdefault("Range", "bytes=0-%d" % max(0, int(nbytes) - 1))
    hdrs.setdefault("Accept-Encoding", "identity")

    attempts = [""]
    px = _env_proxy()
    if px:
        attempts.append(px)

    last = {"ok": False, "ms": 0, "kbps": 0.0, "bytes": 0, "code": 0, "error": ""}
    for proxy in attempts:
        for insecure in (False, True):
            t0 = time.time()
            got, code = 0, 0
            try:
                op, req = _open(url, timeout, hdrs, proxy, insecure)
                with op.open(req, timeout=timeout) as r:
                    code = getattr(r, "status", 200)
                    while got < nbytes:
                        buf = r.read(min(65536, nbytes - got))
                        if not buf:
                            break
                        got += len(buf)
                    # 到了就主动断开：大文件不要继续拉
                dt = max(0.001, time.time() - t0)
                kbps = (got / 1024.0) / dt if got else 0.0
                last = {"ok": bool(got), "ms": int(dt * 1000),
                        "kbps": round(kbps, 1), "bytes": got, "code": code,
                        "error": "" if got else "没读到数据", "via_proxy": bool(proxy),
                        "insecure": insecure}
                return last
            except urllib.error.HTTPError as e:
                # 416/403 之类：能连上，但这条线不给这段数据
                dt = max(0.001, time.time() - t0)
                last = {"ok": False, "ms": int(dt * 1000), "kbps": 0.0, "bytes": got,
                        "code": getattr(e, "code", 0), "error": "HTTP %s" % getattr(e, "code", "?"),
                        "via_proxy": bool(proxy), "insecure": insecure}
                break                      # 代理解决不了 HTTP 状态码，直接换协议层
            except ssl.SSLCertVerificationError as e:
                last = {"ok": False, "ms": int((time.time() - t0) * 1000), "kbps": 0.0,
                        "bytes": 0, "code": 0, "error": "证书校验失败: %s" % str(e)[:60],
                        "via_proxy": bool(proxy), "insecure": insecure}
                continue                   # 换 insecure 再试一次
            except Exception as e:
                last = {"ok": False, "ms": int((time.time() - t0) * 1000), "kbps": 0.0,
                        "bytes": 0, "code": 0,
                        "error": "%s: %s" % (type(e).__name__, str(e)[:70]),
                        "via_proxy": bool(proxy), "insecure": insecure}
                break                      # 换线路（代理）
    return last


def fetch(url: str, timeout: float = 15, headers: dict | None = None,
          limit: int = 4 * 1024 * 1024) -> bytes:
    """取一个不太大的资源（JSON / 小文件）。直连优先，失败再走系统代理。"""
    attempts = [""]
    px = _env_proxy()
    if px:
        attempts.append(px)
    err = None
    for proxy in attempts:
        for insecure in (False, True):
            try:
                op, req = _open(url, timeout, headers, proxy, insecure)
                with op.open(req, timeout=timeout) as r:
                    data = r.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("响应超过 %d 字节上限" % limit)
                return data
            except ssl.SSLCertVerificationError as e:
                err = e
                continue
            except Exception as e:
                err = e
                break
    raise err if err else RuntimeError("请求失败")


def rank(candidates, nbytes: int = DEFAULT_BYTES, timeout: float = DEFAULT_TIMEOUT,
         workers: int = 4) -> list:
    """并行测速并排序。

    candidates: [{"label": 线路名, "url": 地址, "headers": {...}(可选)}]
    返回同样结构的列表（附加 ok/kbps/ms/error），成功的按速度从快到慢排前面，
    失败的保持原顺序排在后面 —— 探测失败也留着，避免「不支持 Range 的镜像」被误杀。
    """
    cands = [c for c in (candidates or []) if isinstance(c, dict) and c.get("url")]
    if not cands:
        return []

    def one(c):
        r = probe(c["url"], nbytes=nbytes, timeout=timeout, headers=c.get("headers"))
        out = dict(c)
        out.update(r)
        return out

    try:
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers or 4), len(cands)))) as ex:
            res = list(ex.map(one, cands))
    except Exception:
        res = [one(c) for c in cands]

    good = [x for x in res if x.get("ok")]
    bad = [x for x in res if not x.get("ok")]
    good.sort(key=lambda x: (-(x.get("kbps") or 0), x.get("ms") or 0))
    return good + bad


def best(candidates, **kw) -> dict | None:
    r = rank(candidates, **kw)
    return r[0] if r else None
