#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RVC 服务状态面板
- 列出本机所有 IPv4 地址（含热点网段），直接给出插件该填的地址
- 列出当前对外开放的 TCP 端口
- 探测 RVC WebUI(7865) / RVC API(3333) 是否存活，读出模型与索引列表
- 显示显卡占用
零第三方依赖，任意 Python 3.8+ 可跑。
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_PORT = 8899
RVC_ROOT = r"D:\RVC-WebUI"
WEBUI_PORT = 7865
API_PORT = 3333
CACHE_SEC = 4.0

CREATE_NO_WINDOW = 0x08000000

KNOWN_PORTS = {
    PANEL_PORT: ("RVC 状态面板", "本页面", True),
    API_PORT: ("RVC 翻唱 API", "AstrBot 插件填这个", True),
    WEBUI_PORT: ("RVC WebUI", "训练 / 推理界面", True),
    3389: ("远程桌面", "", False),
    445: ("SMB 共享", "", False),
    139: ("NetBIOS", "", False),
    135: ("RPC", "", False),
}


# ---------------------------------------------------------------- 基础工具

def run_cmd(args, timeout=4.0):
    """执行命令，自动处理 Windows 中文编码。"""
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        raw = p.stdout or b""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", "ignore")
    except Exception:
        return ""


def port_open(port, host="127.0.0.1", timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def post_json(url, payload, timeout=3.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


# ---------------------------------------------------------------- 网络地址

def classify(ip):
    """返回 (名称, 类别, 提示)"""
    if ip.startswith("192.168.137."):
        return ("Windows 移动热点", "hotspot", "热点网段 · 手机填这个")
    if ip.startswith("192.168.43.") or ip.startswith("192.168.42."):
        return ("安卓热点", "hotspot", "热点网段 · 手机填这个")
    if ip.startswith("192.168."):
        return ("局域网 (路由器)", "lan", "同一 Wi-Fi / 交换机下可用")
    if ip.startswith("10."):
        return ("内网 10.x", "lan", "同一网络下可用")
    if re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
        return ("内网 172.16-31.x", "lan", "同一网络下可用")
    if ip.startswith("192.168.56."):
        return ("VirtualBox 虚拟网卡", "virtual", "手机连不上")
    if ip.startswith("127."):
        return ("本机回环", "loopback", "只能在电脑上访问")
    return ("其他", "other", "")


def get_ips():
    out = run_cmd(["ipconfig"], 5.0)
    found = []
    iface = ""
    for line in out.splitlines():
        s = line.rstrip()
        if s and not s.startswith((" ", "\t")):
            low = s.lower()
            if "适配器" in s or "adapter" in low:
                iface = s.rstrip(":").strip()
        if "IPv4" in s:
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", s)
            if not m:
                continue
            ip = m.group(1)
            if ip.startswith("169.254."):
                continue
            found.append((ip, iface))

    seen, res = set(), []
    for ip, iface in found:
        if ip in seen:
            continue
        seen.add(ip)
        name, kind, hint = classify(ip)
        res.append(
            {"ip": ip, "iface": iface or "未知接口", "kind": kind, "name": name, "hint": hint}
        )

    # 推荐顺序：热点 > 物理局域网 > 其他 > 虚拟网卡
    prio = {"hotspot": 0, "lan": 1, "other": 2, "virtual": 3, "loopback": 4}
    res.sort(key=lambda x: prio.get(x["kind"], 5))
    if res and res[0]["kind"] in ("hotspot", "lan"):
        res[0]["recommend"] = True
    elif res:
        res[0]["recommend"] = False
    return res


# ---------------------------------------------------------------- 端口扫描

def get_ports():
    out = run_cmd(["netstat", "-ano"], 6.0)
    rows = []
    for line in out.splitlines():
        m = re.match(
            r"\s*TCP\s+(\S+):(\d+)\s+\S+\s+(?:LISTENING|监听)\s+(\d+)\s*$", line
        )
        if not m:
            continue
        rows.append((m.group(1), int(m.group(2)), int(m.group(3))))

    best = {}
    for host, port, pid in rows:
        if host in ("0.0.0.0", "[::]"):
            score = 2
        elif host in ("127.0.0.1", "[::1]"):
            score = 0
        else:
            score = 1
        cur = best.get(port)
        if cur is None or score > cur[0]:
            best[port] = (score, host, pid)

    if not best:  # netstat 不可用时用已知端口兜底
        for p in KNOWN_PORTS:
            if port_open(p):
                best[p] = (2, "0.0.0.0", 0)

    res = []
    for port, (score, host, pid) in best.items():
        label, role, key = KNOWN_PORTS.get(port, ("其他程序", "", False))
        res.append(
            {
                "port": port,
                "host": host,
                "pid": pid,
                "external": score == 2,
                "local_only": score == 0,
                "label": label,
                "role": role,
                "known": port in KNOWN_PORTS,
                "key": key,
            }
        )
    res.sort(key=lambda x: (0 if x["known"] else 1, 0 if x["external"] else 1, x["port"]))
    return res


# ---------------------------------------------------------------- RVC 状态

def rvc_status():
    st = {
        "webui_port": port_open(WEBUI_PORT),
        "api_port": port_open(API_PORT),
        "models": [],
        "indices": [],
        "note": "",
    }

    if st["webui_port"]:
        try:
            d = post_json(
                "http://127.0.0.1:%d/run/infer_refresh" % WEBUI_PORT, {"data": []}, 4.0
            )
            arr = d.get("data") or []
            if arr and isinstance(arr[0], list):
                st["models"] = [x for x in arr[0] if isinstance(x, str)]
            if len(arr) > 1 and isinstance(arr[1], list):
                st["indices"] = [os.path.basename(x) for x in arr[1] if isinstance(x, str)]
        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg:
                st["note"] = "WebUI 响应超时，可能正在推理"
            else:
                st["note"] = "WebUI 接口无响应：%s" % type(e).__name__

    # 索引与模型：接口拿不到就读文件系统
    idx = set(st["indices"])
    for sub in ("logs", os.path.join("assets", "indices")):
        try:
            for f in os.listdir(os.path.join(RVC_ROOT, sub)):
                if f.lower().endswith(".index"):
                    idx.add(f)
        except Exception:
            pass
    st["indices"] = sorted(idx)

    if not st["models"]:
        try:
            for f in os.listdir(os.path.join(RVC_ROOT, "assets", "weights")):
                if f.lower().endswith(".pth"):
                    st["models"].append(f)
        except Exception:
            pass

    st["ready"] = bool(st["webui_port"] and st["api_port"] and st["models"])
    return st


def gpu_status():
    out = run_cmd(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        3.0,
    )
    line = out.strip().splitlines()[0] if out.strip() else ""
    if not line:
        return None
    parts = [x.strip() for x in line.split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "name": parts[0],
            "used": float(parts[1]),
            "total": float(parts[2]),
            "temp": int(float(parts[3])),
            "util": int(float(parts[4])),
        }
    except Exception:
        return None


# ---------------------------------------------------------------- 汇总

_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def build_status():
    ips = get_ips()
    ports = get_ports()
    rvc = rvc_status()
    gpu = gpu_status()

    rec = next((x for x in ips if x.get("recommend")), None)
    plugin_url = "http://%s:%d/" % (rec["ip"], API_PORT) if rec else ""

    api_row = next((p for p in ports if p["port"] == API_PORT), None)
    warns = []
    if not rvc["webui_port"]:
        warns.append("RVC WebUI（7865）没在跑 → 双击 D:\\RVC-WebUI\\启动训练界面.bat")
    if not rvc["api_port"]:
        warns.append("RVC 翻唱 API（3333）没在跑 → 双击 D:\\RVCSVC-API\\RVCSVC-API\\启动 rvcapi.bat")
    if api_row and api_row["local_only"]:
        warns.append("3333 只绑定在 127.0.0.1，手机连不上 → 检查 app_rvc.py 里 server_name 是否为 0.0.0.0")
    if not rec:
        warns.append("没检测到可用的局域网地址 → 电脑可能没连热点或 Wi-Fi")
    if rvc["api_port"] and "deepseek.pth" not in rvc["models"] and rvc["models"]:
        warns.append("模型列表里没有 deepseek.pth，检查 assets/weights 目录")

    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "hostname": socket.gethostname(),
        "ready": rvc["ready"],
        "plugin_url": plugin_url,
        "recommend_ip": rec["ip"] if rec else "",
        "panel_url": "http://%s:%d/" % (rec["ip"], PANEL_PORT) if rec else "",
        "ips": ips,
        "ports": ports,
        "rvc": rvc,
        "gpu": gpu,
        "warnings": warns,
    }


def get_status():
    with _lock:
        fresh = _cache["data"] and (time.time() - _cache["ts"] < CACHE_SEC)
        if fresh:
            return _cache["data"]
    data = build_status()
    with _lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return data


# ---------------------------------------------------------------- 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>RVC 服务面板</title>
<style>
:root{
  --bg:#0a0e14;--card:#121821;--card2:#161e29;--line:rgba(255,255,255,.075);
  --txt:#e8edf5;--dim:#8b98ab;--ok:#22c55e;--bad:#64748b;--warn:#f59e0b;--acc:#38bdf8;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{
  margin:0;padding:18px 14px 40px;min-height:100vh;color:var(--txt);
  font:15px/1.55 -apple-system,"Segoe UI","Noto Sans SC","Microsoft YaHei",system-ui,sans-serif;
  background:
    radial-gradient(900px 520px at 12% -12%,rgba(56,189,248,.11),transparent 62%),
    radial-gradient(700px 420px at 96% 0%,rgba(34,197,94,.08),transparent 58%),
    var(--bg);
}
.wrap{max-width:680px;margin:0 auto}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.logo{
  width:44px;height:44px;flex:none;border-radius:13px;display:grid;place-items:center;
  font:800 13px/1 ui-monospace,Consolas,monospace;letter-spacing:.5px;color:#0a0e14;
  background:linear-gradient(145deg,#7dd3fc,#22c55e);box-shadow:0 6px 20px rgba(56,189,248,.28)
}
h1{font-size:17px;margin:0;letter-spacing:.2px}
#sub{font-size:12.5px;color:var(--dim);margin:2px 0 0;font-family:ui-monospace,Consolas,monospace}
.badge{
  margin-left:auto;flex:none;font-size:12.5px;font-weight:700;padding:7px 13px;border-radius:999px;
  border:1px solid transparent;white-space:nowrap
}
.badge.ok{background:rgba(34,197,94,.14);color:#4ade80;border-color:rgba(34,197,94,.28)}
.badge.warn{background:rgba(245,158,11,.14);color:#fbbf24;border-color:rgba(245,158,11,.28)}
.badge.bad{background:rgba(100,116,139,.16);color:#94a3b8;border-color:rgba(100,116,139,.3)}
.badge.wait{background:rgba(56,189,248,.12);color:#7dd3fc;border-color:rgba(56,189,248,.24)}
.card{
  background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);
  border-radius:16px;padding:15px 16px;margin-bottom:12px
}
.card.hero{
  border-color:rgba(56,189,248,.28);
  background:linear-gradient(150deg,rgba(56,189,248,.13),rgba(34,197,94,.06) 55%,var(--card));
  box-shadow:0 10px 30px rgba(0,0,0,.35)
}
.hero .lbl{font-size:11.5px;letter-spacing:.1em;color:#7dd3fc;font-weight:700;margin-bottom:8px}
.addr{
  font:600 20px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;color:#fff;
  word-break:break-all;letter-spacing:-.2px
}
.hero .hint{font-size:12.5px;color:var(--dim);margin-top:7px}
h2{
  font-size:11.5px;font-weight:700;letter-spacing:.1em;color:var(--dim);
  margin:0 0 11px;display:flex;align-items:center;gap:7px
}
.count{font-size:11px;color:#7dd3fc;background:rgba(56,189,248,.1);padding:1px 7px;border-radius:999px;font-weight:600}
.row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed rgba(255,255,255,.055)}
.row:last-child{border-bottom:none;padding-bottom:0}
.row:first-child{padding-top:0}
.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--bad)}
.dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(34,197,94,.15)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(245,158,11,.15)}
.grow{flex:1;min-width:0}
.ip{font:600 16px/1.3 ui-monospace,Consolas,monospace;letter-spacing:-.2px}
.sub{font-size:12px;color:var(--dim);margin-top:2px}
.port{font:700 17px/1 ui-monospace,Consolas,monospace;min-width:56px;color:#fff}
.tag{
  font-size:10.5px;padding:2.5px 8px;border-radius:999px;font-weight:600;white-space:nowrap;
  background:rgba(56,189,248,.12);color:#7dd3fc;border:1px solid rgba(56,189,248,.22)
}
.tag.key{background:rgba(34,197,94,.13);color:#4ade80;border-color:rgba(34,197,94,.25)}
.copy{
  flex:none;font:600 12px/1 inherit;padding:7px 11px;border-radius:9px;cursor:pointer;
  background:rgba(255,255,255,.06);color:#c7d2e0;border:1px solid var(--line)
}
.copy:active{background:rgba(56,189,248,.2);color:#fff}
.kv .row{border-bottom:1px dashed rgba(255,255,255,.055)}
.kv .k{font-size:13.5px;color:var(--dim);flex:none}
.kv .v{font-size:13.5px;font-weight:600;text-align:right;word-break:break-all}
.v.ok{color:#4ade80}
.v.bad{color:#f87171}
.v.warn{color:#fbbf24}
ul{margin:0;padding-left:18px}
li{font-size:13px;color:#fcd34d;margin:5px 0}
footer{display:flex;align-items:center;gap:12px;margin-top:16px}
#refresh{
  flex:none;font:600 13px/1 inherit;padding:10px 16px;border-radius:11px;cursor:pointer;
  background:rgba(56,189,248,.14);color:#7dd3fc;border:1px solid rgba(56,189,248,.26)
}
#refresh:active{background:rgba(56,189,248,.26)}
#ts{font-size:11.5px;color:#5c6878;font-family:ui-monospace,Consolas,monospace}
#toast{
  position:fixed;left:50%;bottom:26px;transform:translate(-50%,20px);opacity:0;pointer-events:none;
  background:rgba(34,197,94,.95);color:#04140a;font-weight:700;font-size:13px;
  padding:10px 18px;border-radius:999px;transition:.22s;z-index:9
}
#toast.on{opacity:1;transform:translate(-50%,0)}
@media (max-width:400px){
  .addr{font-size:17px}.port{font-size:15px;min-width:48px}.copy{display:none}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">RVC</div>
    <div class="grow">
      <h1>服务状态面板</h1>
      <p id="sub">正在读取…</p>
    </div>
    <div id="badge" class="badge wait">检测中</div>
  </header>

  <section class="card hero">
    <div class="lbl">ASTRBOT 插件填这个地址</div>
    <div class="addr" id="plugin-url">—</div>
    <div class="hint" id="plugin-hint">正在探测本机网络…</div>
  </section>

  <section class="card">
    <h2>本机地址 <span class="count" id="ip-count">0</span></h2>
    <div id="ips"></div>
  </section>

  <section class="card">
    <h2>对外开放的端口 <span class="count" id="ext-count">0</span></h2>
    <div id="ports"></div>
    <div class="sub" id="ports-foot" style="margin-top:9px"></div>
  </section>

  <section class="card kv">
    <h2>RVC 引擎</h2>
    <div id="rvc"></div>
  </section>

  <section class="card kv">
    <h2>显卡</h2>
    <div id="gpu"></div>
  </section>

  <section class="card" id="warn-card" style="border-color:rgba(245,158,11,.25)" hidden>
    <h2 style="color:#fbbf24">需要注意</h2>
    <ul id="warns"></ul>
  </section>

  <footer>
    <button id="refresh">立即刷新</button>
    <span id="ts"></span>
  </footer>
</div>
<div id="toast">已复制</div>
<script>
var $ = function (s) { return document.querySelector(s); };

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function toast(msg) {
  var t = $('#toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(toast._t);
  toast._t = setTimeout(function () { t.classList.remove('on'); }, 1400);
}

function copyText(txt) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(txt).then(function () { toast('已复制 ' + txt); },
      function () { fallbackCopy(txt); });
  } else {
    fallbackCopy(txt);
  }
}
function fallbackCopy(txt) {
  var ta = document.createElement('textarea');
  ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); toast('已复制 ' + txt); }
  catch (e) { toast('复制失败，请手动选择'); }
  document.body.removeChild(ta);
}

function dot(cls) { return el('span', 'dot ' + cls); }

function renderIps(d) {
  var box = $('#ips');
  box.innerHTML = '';
  $('#ip-count').textContent = d.ips.length;
  if (!d.ips.length) {
    box.appendChild(el('div', 'sub', '没有检测到局域网地址'));
    return;
  }
  d.ips.forEach(function (x) {
    var row = el('div', 'row');
    row.appendChild(dot(x.recommend ? 'ok' : ''));
    var mid = el('div', 'grow');
    mid.appendChild(el('div', 'ip', x.ip));
    mid.appendChild(el('div', 'sub', x.iface + ' · ' + x.name));
    row.appendChild(mid);
    if (x.recommend) row.appendChild(el('span', 'tag key', '推荐'));
    var b = el('button', 'copy', '复制');
    b.setAttribute('data-copy', 'http://' + x.ip + ':' + d.api_port_guess + '/');
    row.appendChild(b);
    box.appendChild(row);
  });
}

function renderPorts(d) {
  var box = $('#ports');
  box.innerHTML = '';
  var ext = d.ports.filter(function (p) { return p.external; });
  $('#ext-count').textContent = ext.length;
  ext.forEach(function (p) {
    var row = el('div', 'row');
    row.appendChild(dot(p.known ? 'ok' : 'warn'));
    row.appendChild(el('div', 'port', String(p.port)));
    var mid = el('div', 'grow');
    mid.appendChild(el('div', 'ip', p.label));
    mid.appendChild(el('div', 'sub', p.role ? p.role + ' · PID ' + p.pid : 'PID ' + p.pid));
    row.appendChild(mid);
    if (p.key) row.appendChild(el('span', 'tag key', '本机服务'));
    box.appendChild(row);
  });
  if (!ext.length) box.appendChild(el('div', 'sub', '当前没有对外开放的端口'));

  var localOnly = d.ports.filter(function (p) { return p.local_only; }).length;
  var foot = '全部监听端口 ' + d.ports.length + ' 个';
  if (localOnly) foot += '，其中 ' + localOnly + " 个仅绑定 127.0.0.1（只能在电脑上访问）";
  $('#ports-foot').textContent = foot;
}

function kvRow(k, v, cls) {
  var row = el('div', 'row');
  row.appendChild(el('span', 'k', k));
  var val = el('span', 'v ' + (cls || ''), v);
  val.style.marginLeft = 'auto';
  row.appendChild(val);
  return row;
}

function renderRvc(d) {
  var box = $('#rvc');
  box.innerHTML = '';
  var r = d.rvc;
  box.appendChild(kvRow('WebUI 7865', r.webui_port ? '运行中' : '未启动', r.webui_port ? 'ok' : 'bad'));
  box.appendChild(kvRow('翻唱 API 3333', r.api_port ? '运行中' : '未启动', r.api_port ? 'ok' : 'bad'));
  box.appendChild(kvRow('可用模型', r.models.length ? r.models.join('、') : '无',
    r.models.length ? 'ok' : 'bad'));
  box.appendChild(kvRow('检索索引', r.indices.length ? r.indices.join('、') : '无',
    r.indices.length ? 'ok' : 'warn'));
  box.appendChild(kvRow('整体状态', r.ready ? '可以唱' : '还不能用', r.ready ? 'ok' : 'warn'));
  if (r.note) box.appendChild(kvRow('提示', r.note, 'warn'));
}

function renderGpu(d) {
  var box = $('#gpu');
  box.innerHTML = '';
  if (!d.gpu) { box.appendChild(kvRow('状态', '未检测到 NVIDIA 显卡', 'bad')); return; }
  var g = d.gpu;
  box.appendChild(kvRow('型号', g.name));
  box.appendChild(kvRow('显存', g.used + ' / ' + g.total + ' MB'));
  box.appendChild(kvRow('温度', g.temp + ' °C'));
  box.appendChild(kvRow('占用率', g.util + ' %'));
}

function render(d) {
  var badge = $('#badge');
  badge.className = 'badge ' + (d.ready ? 'ok' : 'bad');
  badge.textContent = d.ready ? '正常' : '异常';
  $('#sub').textContent = d.hostname + ' · ' + d.date + ' ' + d.time;

  if (d.plugin_url) {
    $('#plugin-url').textContent = d.plugin_url;
    $('#plugin-hint').textContent = '手机先连同一个热点 / Wi-Fi，把这个地址填进「RVC API Gradio 后端服务地址」';
  } else {
    $('#plugin-url').textContent = '未连接网络';
    $('#plugin-hint').textContent = '电脑当前没有可用的局域网地址';
  }

  d.api_port_guess = 3333;
  renderIps(d);
  renderPorts(d);
  renderRvc(d);
  renderGpu(d);

  var warns = d.warnings || [];
  $('#warn-card').hidden = !warns.length;
  var ul = $('#warns');
  ul.innerHTML = '';
  warns.forEach(function (w) { ul.appendChild(el('li', null, w)); });

  $('#ts').textContent = '更新于 ' + d.time;
}

function load() {
  fetch('/api/status?t=' + Date.now(), { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function () {
      var b = $('#badge');
      b.className = 'badge bad';
      b.textContent = '断开';
      $('#sub').textContent = '面板程序已停止';
    });
}

document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-copy]');
  if (b) { copyText(b.getAttribute('data-copy')); return; }
  if (e.target.id === 'plugin-url' && e.target.textContent.indexOf('http') === 0) {
    copyText(e.target.textContent);
  }
});
$('#refresh').addEventListener('click', load);

load();
setInterval(load, 4000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                data = get_status()
            except Exception as e:
                data = {"time": datetime.now().strftime("%H:%M:%S"), "date": "",
                        "hostname": "-", "ready": False, "plugin_url": "",
                        "ips": [], "ports": [], "warnings": ["面板内部错误: %s" % e],
                        "rvc": {"webui_port": False, "api_port": False, "models": [],
                                "indices": [], "note": "", "ready": False},
                        "gpu": None}
            self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    srv.daemon_threads = True
    print("=" * 56)
    print(" RVC 服务状态面板已启动")
    print("=" * 56)
    print(" 本机访问 : http://127.0.0.1:%d/" % PANEL_PORT)
    for item in get_ips():
        print(" 手机访问 : http://%s:%d/   (%s)" % (item["ip"], PANEL_PORT, item["name"]))
    print("-" * 56)
    print(" 保持此窗口开着；关掉窗口面板即停止。")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
