import re, os, time
import math

# ============ 线程数自适应（必须在 numpy / torch 之前） ============
# 这几个库导入时就会按当前环境建线程池，导入之后再设就没用了。
# 面板会按物理核数塞好（perf.py），这里只是「直接跑脚本」时的兜底：
# 逻辑核全开在 Ryzen 这种 SMT 机器上反而更慢（抢内存带宽、和推理抢核心）。
if not os.environ.get("OMP_NUM_THREADS"):
    _n_cpu = min(8, max(2, (os.cpu_count() or 4) // 2))
    for _k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
               "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(_k, str(_n_cpu))

import gc
import requests
import json
import shutil
import argparse
from difflib import SequenceMatcher

parser = argparse.ArgumentParser()
parser.add_argument(
    '--is_nohalf', action='store_true'
)
a = parser.parse_args()
is_half=not a.is_nohalf

# ============ torch 延迟导入 ============
# import torch 要 10 秒以上，而启动阶段其实一处都用不到：
# 设备检测只在「分离模型现场加载」时需要，清显存只在任务结束后需要。
# 推迟到第一次真正用到再导，3333 从双击到就绪能快一大截。
_torch = None


def _get_torch():
    global _torch
    if _torch is None:
        import torch as _t
        _torch = _t
    return _torch


def _detect_device():
    return "cuda" if _get_torch().cuda.is_available() else "cpu"

headers = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
}
pattern = r'//www\.bilibili\.com/video[^"]*'
models=[]
index=[]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RVC_API_BASE = "http://127.0.0.1:7865"
# 兄弟目录里的 WebUI（E:\RVC-Data\RVCSVC\RVCSVC-API → E:\RVC-Data\RVC-WebUI）
_RVC_WEBUI_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", "RVC-WebUI"))
_WEBUI_LOG = os.path.join(_RVC_WEBUI_DIR, "logs", "webui_service.log")


def _webui_tail_error(max_bytes=131072):
  """从 WebUI 日志尾部捞出最后一条异常，让报错能落地而不是只有「推理失败」。

  gradio 的 /run/infer_convert 出错时返回 HTTP 500 + {"error": null}，
  真实堆栈只写在这个日志里；不抓出来用户根本不知道该改什么。
  """
  try:
    if not os.path.isfile(_WEBUI_LOG):
      return ""
    with open(_WEBUI_LOG, "rb") as f:
      f.seek(0, os.SEEK_END)
      size = f.tell()
      f.seek(max(0, size - max_bytes))
      tail = f.read().decode("utf-8", "ignore")
  except Exception:
    return ""

  lines = tail.splitlines()
  # 异常行形如 "RuntimeError: Error(s) in loading ..."，排除 Traceback 里 "File ..." 之类
  for i in range(len(lines) - 1, -1, -1):
    s = lines[i].strip()
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Interrupt):\s*\S", s):
      detail = [s]
      # 带上紧随其后的尺寸不符明细，这类信息对用户最有价值
      for nxt in lines[i + 1:i + 4]:
        n = nxt.strip()
        if not n or n.startswith("Traceback") or re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):", n):
          break
        detail.append(n)
      msg = " ".join(detail)
      return msg[:400] + ("…" if len(msg) > 400 else "")
  return ""


# ========== 新增：音高优化函数 ==========
def optimize_pitch_shift(key_shift):
    """
    将升降调优化到最小调整幅度，保证最佳音质
    例如：+11 转为 -1，-10 转为 +2
    """
    if key_shift > 6:
        return key_shift - 12
    elif key_shift < -6:
        return key_shift + 12
    else:
        return key_shift
# ======================================

# ===== 音频下载：优先自建本地网易云接口(3001)，逐源校验文件头 =====
NCM_LOCAL = "http://127.0.0.1:3002"
# 本机回环必须绕开系统代理，否则请求会被代理软件截走（502 / WinError 10061）
_LOCAL_HTTP = requests.Session()
_LOCAL_HTTP.trust_env = False

# mp3(ID3/帧同步) / wav(RIFF) / flac(fLaC) / ogg(OggS) / m4a(ftyp@offset4)
_AUDIO_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa",
                b"RIFF", b"OggS", b"fLaC")

def _looks_like_audio(data):
  """公共源经常返回 HTML 页面/JSON 错误却带 200，必须靠文件头识别"""
  if not data or len(data) < 4096:
    return False
  head = data[:16]
  if any(head.startswith(m) for m in _AUDIO_MAGIC):
    return True
  if head[4:8] == b"ftyp":   # m4a / mp4
    return True
  return False

def _ncm_local_urls(song_id):
  """从自建 3001 拿直链，v1 优先（给高码率），老接口兜底。
  返回 (候选列表, 接口是否连得上)——用于失败时区分“服务没开”和“歌没版权”。"""
  urls, reached = [], False
  for api in ("/song/url/v1?id=%s&level=exhigh", "/song/url?id=%s&br=320000"):
    try:
      r = _LOCAL_HTTP.get(NCM_LOCAL + (api % song_id), timeout=12)
      reached = True
      payload = r.json()
      rows = payload.get("data") or []
      if isinstance(rows, list) and rows:
        u = rows[0].get("url")
        if u:
          urls.append(("本地网易云" + api.split("?")[0], u))
        else:
          print(f"ℹ️ {api.split('?')[0]} 未返回直链（code={rows[0].get('code')}，多为 VIP/无版权）")
    except Exception as e:
      print(f"⚠️ 本地网易云接口 {api.split('?')[0]} 不可用: {type(e).__name__}: {e}")
  return urls, reached

def _candidate_sources(song_id, local=None):
  """按可靠性排序的候选直链"""
  cands = list(local or [])
  # 网易云官方外链（会 302 到真实 mp3）
  cands.append(("网易云官方外链", f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"))
  # 老公共源，靠内容校验兜住它可能返回 HTML 的情况
  try:
    j = requests.get(f"https://api.vkeys.cn/v2/music/netease?id={song_id}", timeout=20).json()
    u = (j.get("data") or {}).get("url")
    if u:
      cands.append(("vkeys", u))
  except Exception:
    pass
  return cands

def _song_real_ms(song_id):
  """查歌曲真实时长（毫秒），用于识别试听片段。查不到返回 0。"""
  try:
    r = _LOCAL_HTTP.get(f"{NCM_LOCAL}/song/detail?ids={song_id}", timeout=8)
    s = (r.json().get("songs") or [{}])[0]
    return int(s.get("dt") or s.get("duration") or 0)
  except Exception:
    return 0

def get_response(song_id):
  sid = str(song_id).strip()
  print(f"开始下载歌曲: {sid}")
  local, reached = _ncm_local_urls(sid)
  tried = []
  for label, url in _candidate_sources(sid, local):
    tried.append(label)
    try:
      resp = requests.get(url, timeout=90, allow_redirects=True)
      data = resp.content or b""
      if resp.status_code == 200 and _looks_like_audio(data):
        print(f"✅ 下载成功（源: {label}, {len(data) / 1048576:.1f} MiB）")
        return resp
      print(f"⚠️ 源 {label} 没给音频（HTTP {resp.status_code}, {len(data)} 字节, "
            f"头部={data[:12]!r}），换下一个")
    except Exception as e:
      print(f"⚠️ 源 {label} 请求失败: {type(e).__name__}: {e}")
  if reached:
    hint = ("本地网易云接口能连上，但没拿到播放地址——这首歌多半是 VIP 专享、"
            "无版权或已下架，换一首试试。")
  else:
    hint = ("本地网易云接口 3001 连不上——打开监控面板 http://127.0.0.1:8899 "
            "点「一键全部启动」再试。")
  raise RuntimeError("下载失败（已尝试: " + " / ".join(tried) + "）。" + hint)

def change_model(model):
  """切换模型"""
  try:
    global index
    response = requests.post(f"{RVC_API_BASE}/run/infer_change_voice", json={
      "data": [
        model,
        0.33,
        0.33,
    ]}, timeout=10).json()
    try:
        cand = response["data"][4]
        # 新版 WebUI 返回的是 {"value": 路径, "__type__": "update"} 字典
        if isinstance(cand, dict):
            cand = cand.get("value")
        if isinstance(cand, str) and cand.strip():
            index = [cand]
            print(f"当前索引: {cand}")
    except Exception:
        pass
    print(f"模型已切换为: {model}")
    return f"✅ 成功切换到模型: {model}"
  except Exception as e:
    print(f"切换模型失败: {e}")
    return f"❌ 切换模型失败: {e}"

def show_model():
  """获取可用模型列表"""
  global models, index
  try:
    response = requests.post(f"{RVC_API_BASE}/run/infer_refresh", json={
      "data": []
    }, timeout=10).json()

    models = response["data"][0]["choices"]
    try:
        index = response["data"][1]["choices"]
    except Exception:
        index = []   # 新版 WebUI 的 infer_refresh 只返回模型列表
    print(f"已加载 {len(models)} 个模型")
    return models
  except Exception as e:
    print(f"获取模型列表失败: {e}")
    return []

RVC_ROOT = os.environ.get("RVC_WEBUI_ROOT", r"D:\RVC-WebUI")

# 环境变量写错/没设时（比如直接跑这个脚本），退回兄弟目录推导出来的真实路径。
# 以前这里写死 D:\RVC-WebUI，装在别的盘时 _collect_indices 扫的目录根本不存在。
if not os.path.isdir(RVC_ROOT):
    RVC_ROOT = _RVC_WEBUI_DIR


def _local_models():
    """直接扫 assets/weights 拿模型列表。

    启动时**不再同步等 WebUI**（它要 10~150 秒才起得来），本地扫是毫秒级；
    WebUI 起来后再由后台线程 refresh_models() 对齐一次（索引路径以它为准）。
    """
    d = os.path.join(RVC_ROOT, "assets", "weights")
    try:
        names = [f for f in os.listdir(d) if f.lower().endswith(".pth")]
    except OSError:
        return []
    return sorted(names, key=lambda s: s.lower())


def _collect_indices():
    """合并来源：WebUI 返回值 + 本地 assets/indices 与 logs 目录扫描"""
    paths = [p for p in index if isinstance(p, str) and p.strip()]
    for d in (os.path.join(RVC_ROOT, "assets", "indices"), os.path.join(RVC_ROOT, "logs")):
        try:
            for fn in os.listdir(d):
                if fn.endswith(".index"):
                    paths.append(os.path.join(d, fn))
        except Exception:
            pass
    seen, out = set(), []
    for p in paths:
        key = os.path.abspath(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _index_key(name):
    """把 index 文件名归一成「模型名」，方便和 pth 对号入座。

    WebUI 训练出来的 index 长这样：
        added_IVF966_Flat_nprobe_1_mygf-tf_v2.index
    前面那串 added_IVF<数字>_Flat_nprobe_<数字>_ 和结尾的 _v2 都只是生成参数，
    真正和模型对应的是中间那段。以前直接拿整串和模型名比相似度，0.32 就判成
    「没找到」，明明配套的 index 就在旁边，index_rate 白白不起作用。
    """
    n = re.sub(r"^added_IVF\d+_Flat_nprobe_\d+_", "", name, flags=re.I)
    n = re.sub(r"(_v\d+)?(_index)?$", "", n, flags=re.I)
    return n or name


def find_index(model):
    candidates = _collect_indices()
    if not candidates:
        print("索引列表为空，跳过索引（音质会略差）")
        return None

    # 提取模型名（去掉扩展名）
    model_name = os.path.splitext(model)[0].lower()

    # 计算每个 index 文件的相似度
    best_match = None
    best_score = 0
    threshold = 0.4

    for index_path in candidates:
        # 提取 index 文件名（去掉路径和扩展名）
        index_name = os.path.splitext(os.path.basename(index_path))[0].lower()

        # 去掉训练参数前缀再比；整串完全对上就直接算 1.0，
        # 名字里包含模型名（如 mygf 与 mygf-tf）给 0.9，避免被前缀拉低
        key = _index_key(index_name)
        if key == model_name:
            score = 1.0
        else:
            score = max(
                SequenceMatcher(None, model_name, key).ratio(),
                SequenceMatcher(None, model_name, index_name).ratio(),
            )
            if model_name and model_name in index_name:
                score = max(score, 0.9)

        if score > best_score:
            best_score = score
            best_match = index_path
    if best_score < threshold:
        print(f"未找到匹配的 index（最高相似度: {best_score:.2f}）")
        return None
    if best_match:
        # 绝对路径直接用；相对路径按 WebUI 根目录解析
        if not os.path.isabs(best_match):
            best_match = os.path.normpath(os.path.join(RVC_ROOT, best_match))
        print(f"找到匹配: {best_match}（相似度: {best_score:.2f}）")
    return best_match
    

weight_uvr5_root = "uvr5/uvr_model"
uvr5_names = []
for name in os.listdir(weight_uvr5_root):
    if name.endswith(".pth") or "onnx" in name:
        uvr5_names.append(name.replace(".pth", ""))

# 注：AudioPre（uvr5.vr 里）原来在模块顶层导入 —— 这一句会连带把 torch、
# librosa 整条链拖进来，白白拖慢启动。已并入 _load_pre_fun 用时再导。

# ============ 分离模型改为「用时才加载」 ============
# 以前进程一起来就把 5_HP-Karaoke 分离器装进内存（几百 MB 级），声线越装越多、
# 内存越来越紧的机器上，这份内存从启动一直白占到最后一个任务。
# 现在：启动不加载，第一次需要分离的任务现场加载（日志会打「按需加载」）；
# 空闲超过 RVC_UVR_IDLE_UNLOAD 秒（默认 15 分钟，0=从不卸载）自动释放，
# 下个任务再装回来。加载/释放都在同一把锁后面，并发任务不会撞车。
import threading as _threading

_uvr5_lock = _threading.Lock()
_uvr5 = {
    "fun": None,                        # AudioPre 实例，None = 未加载
    "model": "5_HP-Karaoke-UVR.pth",    # 要用的分离模型名（或绝对路径）
}
_uvr5_last_used = [0.0]               # 最近一次取用时间（锁内读写）
UVR_IDLE_UNLOAD = float(os.environ.get("RVC_UVR_IDLE_UNLOAD", "900"))


def _uvr5_path(model_name):
    return model_name if os.path.isabs(model_name) else os.path.join(weight_uvr5_root, model_name)


def _load_pre_fun(model_name):
    path = _uvr5_path(model_name)
    print(f"⏳ 按需加载人声分离模型: {os.path.basename(path)}（这一步完成后才开始分离）")
    t0 = time.time()
    from uvr5.vr import AudioPre  # 用时才导：会连带 torch/librosa，启动阶段不付这笔钱
    f = AudioPre(agg=10, model_path=path, device=_detect_device(), is_half=is_half)
    print(f"✅ 分离模型就绪（{time.time() - t0:.1f} 秒）")
    return f


def get_pre_fun():
    """取当前分离器；没加载就现场加载（线程安全），并记下使用时间。"""
    with _uvr5_lock:
        if _uvr5["fun"] is None:
            _uvr5["fun"] = _load_pre_fun(_uvr5["model"])
        _uvr5_last_used[0] = time.time()
        return _uvr5["fun"]


def release_pre_fun(reason=""):
    """释放分离模型腾内存。返回是否真的释放了。"""
    with _uvr5_lock:
        if _uvr5["fun"] is None:
            return False
        _uvr5["fun"] = None
        gc.collect()
        try:
            t = _get_torch()
            if t.cuda.is_available():
                t.cuda.empty_cache()
        except Exception:
            pass
    print(f"🧹 已释放人声分离模型，腾出内存（{reason}）")
    return True


def _uvr5_idle_watcher():
    """后台线程：空闲太久就把分离模型卸掉，把内存让给后面的任务。"""
    if UVR_IDLE_UNLOAD <= 0:
        return
    while True:
        time.sleep(60)
        with _uvr5_lock:
            if _uvr5["fun"] is None:
                continue
            idle = time.time() - _uvr5_last_used[0]
        if idle >= UVR_IDLE_UNLOAD:
            release_pre_fun("空闲超过 %.0f 分钟" % (UVR_IDLE_UNLOAD / 60))


_threading.Thread(target=_uvr5_idle_watcher, daemon=True,
                  name="uvr5-idle-unload").start()


def ensure_pre_fun(model_name="", agg=10, tta=False, postprocess=False, window_size=512,
                   high_end_process="mirroring"):
  """按插件传入的分离模型与参数准备 UVR5 分离器（不传就用默认；这里不加载，等用时才装）"""
  name = str(model_name or "").strip()
  if name and not name.endswith(".pth"):
    name += ".pth"
  if name:
    path = name if os.path.isabs(name) else os.path.join(weight_uvr5_root, name)
    if not os.path.isfile(path):
      print(f"⚠️ 指定的分离模型不存在，沿用默认模型: {name}")
      name = ""
  if name and name != _uvr5["model"]:
    # 换模型：旧的直接扔掉，下次 get_pre_fun 用新名字现场加载
    with _uvr5_lock:
      had = _uvr5["fun"] is not None
      _uvr5["model"] = name
      _uvr5["fun"] = None
    if had:
      gc.collect()
      print(f"🔄 分离模型已切换为 {os.path.basename(_uvr5_path(name))}（下个任务按需重新加载）")
  # 分离强度、输出文件名都取自 self.data，必须写到实例上（只写 .agg 无效）。
  # 有缓存就直接用；没缓存就现场加载——参数在加载完成后补写，一次到位。
  f = get_pre_fun()
  try:
    f.data["agg"] = int(agg)
    f.data["tta"] = bool(tta)
    f.data["postprocess"] = bool(postprocess)
    f.data["window_size"] = int(window_size)
    f.data["high_end_process"] = str(high_end_process or "mirroring")
    f.agg = int(agg)
  except Exception as e:
    print(f"⚠️ 分离参数写入失败: {e}")
  print(f"🎚️ 人声分离参数: agg={int(agg)} tta={bool(tta)} "
        f"postprocess={bool(postprocess)} window={int(window_size)} "
        f"high_end={high_end_process}")

from pydub import AudioSegment
from pydub.utils import make_chunks
from pydub.effects import compress_dynamic_range
from pydub.effects import normalize as pydub_normalize
from pedalboard import Pedalboard, Compressor, Reverb
from scipy.signal import firwin, lfilter, iirfilter
import os
import numpy as np
import librosa
import soundfile
import gradio as gr
import scipy.signal
if not hasattr(scipy.signal, 'hann'):
    scipy.signal.hann = np.hanning
split_model = "UVR-HP5"


def _avail_mem_mb():
    """当前系统可用物理内存（MB）。只为日志/报错服务，拿不到就返回 -1。"""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return ms.ullAvailPhys / 1048576.0
    except Exception:
        return -1.0
  

def wwy_downloader(
    filename,
    split_model
):
    # 这两个值都会被拼进路径（rvc_<filename>.wav、uvr_<filename>.wav、
    # ./output/<split_model>/<filename>/），而且是**外部请求直接传进来的字符串**。
    # 不清洗的话，split_model = "../../../Users/xxx/Desktop" 就能让下面的 makedirs
    # 和 UVR 把文件写到盘上任意位置。这里统一切成安全字符。
    filename = re.sub(r"[^0-9A-Za-z_.\-]", "", os.path.basename(str(filename or "")))[:120]
    if filename in ("", ".", ".."):
        filename = "song"
    split_model = re.sub(r"[^0-9A-Za-z_.\-]", "_", str(split_model or "UVR-HP5"))[:40]
    if split_model in ("", ".", "..") or ".." in split_model:
        split_model = "UVR-HP5"

    audio_content = get_response(filename).content
    # 1. 下载到带前缀的临时文件，避免冲突
    temp_prefixed_path = "rvc_" + filename.strip() + ".wav"
    with open(temp_prefixed_path, mode="wb") as f:
        f.write(audio_content)
    
    # 2. 从带前缀的文件加载和处理音频
    audio_orig = AudioSegment.from_file(temp_prefixed_path)
    duration_minutes = len(audio_orig) / 60000
    got_ms = len(audio_orig)
    print(f"原始音频时长: {duration_minutes:.2f} 分钟")
    # 匿名状态下载 VIP 曲目时，网易云只返回 30~45 秒试听片段，接口照样 200。
    # 用歌曲真实时长对比才判得准（单看“很短”会把短歌误判）。
    real_ms = _song_real_ms(filename)
    if real_ms and got_ms < real_ms * 0.8:
        print(f"⚠️⚠️ 只下到 {got_ms / 1000:.0f} 秒，但这首歌实际 {real_ms / 1000:.0f} 秒"
              f"（{got_ms / real_ms * 100:.0f}%）——这是【试听片段】，不是整首！")
        print("    这首歌是 VIP/付费曲目，匿名状态只能拿片段。")
        print("    解决办法：把网易云登录 Cookie 存到 "
              "D:\\RVCSVC-API\\ncmapi\\ncm_cookie.txt 后重启 3001 服务。")
    elif got_ms < 35000:
        print(f"⚠️⚠️ 音频只有 {got_ms / 1000:.0f} 秒，可能是试听片段或下载不完整，"
              f"若翻唱结果异常请检查 3001 是否启用了登录态。")
    if duration_minutes > 5:
        print("⚠️ 音频超过5分钟，正在截取前5分钟...")
        audio_orig = audio_orig[:300000]
    
    # 3. 【关键修复】在调用UVR之前，将处理好的音频导出为UVR期望的、不带前缀的文件名
    uvr_input_path = filename.strip() + ".wav"
    audio_orig.export(uvr_input_path, format="wav")
    
    # 4. 删除带前缀的临时文件，我们不再需要它了
    if os.path.isfile(temp_prefixed_path):
        os.remove(temp_prefixed_path)

    # 5. 调用UVR，现在它会生成正确的文件名了
    os.makedirs(f"./output/{split_model}/{filename}/", exist_ok=True)
    pre_fun = get_pre_fun()   # 没加载就现场装（首个任务会多花几秒，之后走缓存）
    print("分离人声伴奏")
    print("   分离前可用内存: %.0f MB" % _avail_mem_mb())
    try:
        pre_fun._path_audio_(uvr_input_path, f"./output/{split_model}/{filename}/", f"./output/{split_model}/{filename}/", "wav")
    except MemoryError:
        # 分离这一步要把整首歌的频谱摊在内存里，5 分钟的歌正常也要 2~3GB。
        # 内存真的不够时 numpy 只会丢一句 "Unable to allocate xx MiB"，
        # 看着像代码 bug，其实是机器扛不住 —— 这里换成能照着做的说明。
        print("   ❌ 内存不足，分离失败（可用 %.0f MB）" % _avail_mem_mb())
        raise RuntimeError(
            "内存不够，处理人声/伴奏这一步失败（这首歌 %.1f 分钟）。\n"
            "  这一步需要 2~3GB 空闲内存，当前电脑不够了。\n"
            "  可以这样做：\n"
            "    1. 关掉浏览器里多余的标签页、游戏、剪辑软件等吃内存的程序，再点一次生成；\n"
            "    2. 或者换一首短一点的歌（3 分钟以内最稳）；\n"
            "    3. 如果是第二次点生成才失败，先点一次「一键停止」再「一键全部启动」，把残留内存清掉。" % duration_minutes
        ) from None
    finally:
        # pydub 的整首歌 PCM 和下载到的原始字节到这里就没用了，
        # 一首 5 分钟的歌它们合计上百 MB，留着只会在后面 RVC 推理时添乱
        audio_orig = None
        audio_content = None
        # 分离用的临时 wav 也要清：失败时不清的话，每次失败都在安装目录
        # 留一份几十 MB 的整首歌，攒几次就把盘吃掉（以前就留过两份）。
        if os.path.isfile(uvr_input_path):
            try:
                os.remove(uvr_input_path)
            except OSError:
                pass
        gc.collect()
    
    # 6. 删除UVR用过的输入文件
    if os.path.isfile(uvr_input_path):
        os.remove(uvr_input_path)

    # 7. 返回正确的文件路径，现在这个文件肯定存在
    #    注意：输出文件名里的数字是 AudioPre.data["agg"]，不是固定 10
    _agg_now = 10
    try:
      _agg_now = int(pre_fun.data["agg"])
    except Exception:
      pass
    return (f"./output/{split_model}/{filename}/vocal_{filename}.wav_{_agg_now}.wav",
            f"./output/{split_model}/{filename}/instrument_{filename}.wav_{_agg_now}.wav")



def _start_inst_shift(inst_path, song_id, key_shift):
    """伴奏变调放后台线程：它只依赖分离结果，和 RVC 推理毫无依赖关系。

    pedalboard 的 PitchShift 一首 5 分钟的歌要 4~5 秒（纯 CPU，PSOLA），
    以前是在推理完之后串行做，白等这 4~5 秒；现在和推理同时跑。
    """
    box = {"audio": None, "err": None, "seconds": 0.0}

    def run():
        t0 = time.time()
        try:
            from pedalboard import Pedalboard, PitchShift
            y, sr_i = librosa.load(inst_path, sr=None)
            y_shifted = Pedalboard([PitchShift(semitones=int(key_shift))])(y, sr_i)
            os.makedirs("temp", exist_ok=True)
            out = os.path.join("temp", "shifted_%s_inst.wav" % song_id)
            soundfile.write(out, y_shifted, sr_i)
            box["audio"] = AudioSegment.from_file(out, format="wav")
        except Exception as e:
            box["err"] = e
        box["seconds"] = time.time() - t0

    t = _threading.Thread(target=run, daemon=True, name="inst-pitch-shift")
    t.start()
    return {"thread": t, "box": box, "path": inst_path}


def _finish_inst_shift(job):
    """等后台变调结束（正常情况下早就跑完了，等于不花时间）。"""
    job["thread"].join(timeout=600)
    if job["box"]["audio"] is not None:
        print("✅ 伴奏音高调整完成（%.1f 秒，已与推理并行）" % job["box"]["seconds"])
        return job["box"]["audio"]
    print("⚠️ 伴奏音高调整失败，使用原始伴奏: %s" % job["box"]["err"])
    return AudioSegment.from_file(job["path"], format="wav")


def convert(song_name_src, key_shift, vocal_vol, inst_vol, model_dropdown,
            reverb_intensity=4, delay_intensity=0,
            uvr5_agg=10, uvr5_model="", split_model="UVR-HP5",
            f0_method="rmvpe", index_rate=0.75, protect=0.33, rms_mix_rate=0.25,
            resample_sr=0, filter_radius=3, output_format="mp3",
            uvr5_hop_length=128, uvr5_window_size=512,
            uvr5_enable_tta=False, uvr5_enable_postprocess=False,
            uvr5_enable_denoise=False, normalize=True,
            # ===== 别名区：不同插件版本命名不一样，全部接收，取第一个有值的 =====
            uvr5_tta=None, uvr5_postprocess=None, uvr5_denoise=None,
            uvr5_hop=None, uvr5_win=None, uvr5_sample_rate=None, uvr5_high_end=None,
            uvr5_format=None, uvr5_high_end_process=None,
            msst_model=None, msst_use_tta=None, msst_batch_size=None,
            msst_num_overlap=None, msst_normalize=None,
            vocal_postprocess=None, shift_accompaniment=None,
            tta=None, postprocess=None, window_size=None, agg=None, denoise=None,
            f0_min=None, f0_max=None, crepe_hop_length=None, speaker_id=None,
            pitch_shift=None, song_id=None, model_name=None, model=None,
            reverb=None, echo=None, delay=None,
            vocal_gain=None, inst_gain=None, sample_rate=None, acapella=None):
  """进行翻唱推理合成（参数取超集 + 别名，兼容不同版本的插件调用）"""
  _mark_job()          # 告诉预热线程「有真活了，让路」
  # 各阶段计时（也打给用户看：卡在哪一步一眼就知道）
  _laps = []
  _t_start = time.time()
  _t_prev = [_t_start]

  def _lap(name):
    now = time.time()
    _laps.append((name, now - _t_prev[0]))
    _t_prev[0] = now
  # song_name_src 与 split_model 都会被拼进 ./output/<split>/<song>/ 这类路径，
  # 而且都是**外部请求直接传的字符串**。这里统一归一化成安全字符，
  # 后面所有路径都用同一个值（不然和 wwy_downloader 的返回值对不上，读不到文件）。
  song_name_src = re.sub(r"[^0-9A-Za-z_.\-]", "",
                         os.path.basename(str(song_name_src or "").strip()))[:120]
  if song_name_src in ("", ".", ".."):
    raise gr.Error("歌曲 ID 不合法（只接受数字 ID）")
  split_model = re.sub(r"[^0-9A-Za-z_.\-]", "_",
                       str(split_model or "UVR-HP5").strip())[:40] or "UVR-HP5"
  if split_model in (".", "..") or ".." in split_model:
    split_model = "UVR-HP5"

  # ==================================================================
  # 数值 / 布尔清洗
  # 插件的 _conf_schema.json 里 index_rate 是 string 类型('0.75')，
  # 若把它绑到 gr.Slider，gradio 的 raise_if_out_of_bounds 会拿 str 和 int
  # 比较而抛 TypeError。所以隐藏组件统一用 Textbox，在函数内做安全转换。
  # ==================================================================
  def _f(v, d=0.0):
    if v is None:
      return float(d)
    if isinstance(v, bool):
      return float(v)
    if isinstance(v, (int, float)):
      return float(v)
    s = str(v).strip()
    if s == "":
      return float(d)
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else float(d)

  def _i(v, d=0):
    return int(round(_f(v, d)))

  def _b(v, d=False):
    if v is None:
      return bool(d)
    if isinstance(v, bool):
      return v
    if isinstance(v, (int, float)):
      return bool(v)
    s = str(v).strip().lower()
    if s in ("", "none", "null", "nan"):
      return bool(d)
    return s in ("1", "true", "yes", "on", "y", "t", "开", "是")

  key_shift = _i(key_shift, 0)
  vocal_vol = _f(vocal_vol, 0.0)
  inst_vol = _f(inst_vol, 0.0)
  reverb_intensity = _f(reverb_intensity, 4.0)
  delay_intensity = _f(delay_intensity, 0.0)
  index_rate = _f(index_rate, 0.75)
  protect = _f(protect, 0.33)
  rms_mix_rate = _f(rms_mix_rate, 0.25)
  resample_sr = _i(resample_sr, 0)
  filter_radius = _i(filter_radius, 3)
  normalize = _b(normalize, True)

  # 清唱模式：显式传 acapella=true，或把伴奏音量压到 -40dB 以下（正常调音量不会这么低）
  _acapella = _b(acapella, False) or inst_vol <= -40.0

  # ---- 别名归一 ----
  if not song_name_src and song_id:
    song_name_src = str(song_id)
  if pitch_shift is not None and key_shift == 0:
    key_shift = _i(pitch_shift, 0)
  if (model_name or model) and not model_dropdown:
    model_dropdown = model_name or model
  if reverb is not None:
    reverb_intensity = _f(reverb, reverb_intensity)
  if echo is not None:
    delay_intensity = _f(echo, delay_intensity)
  if delay is not None:
    delay_intensity = _f(delay, delay_intensity)
  if vocal_gain is not None and vocal_vol == 0:
    vocal_vol = _f(vocal_gain, 0.0)
  if inst_gain is not None and inst_vol == 0:
    inst_vol = _f(inst_gain, 0.0)
  if speaker_id not in (None, "", 0, "0"):
    print(f"ℹ️ 插件传入 speaker_id={speaker_id}（单说话人模型忽略）")
  if _f(f0_min, 0) or _f(f0_max, 0) or _f(crepe_hop_length, 0):
    print(f"ℹ️ f0 范围/步长参数: {f0_min}~{f0_max} hop={crepe_hop_length}（当前用 rmvpe，忽略）")

  # ---- 分离参数：主名与别名谁有值用谁 ----
  agg = max(0, min(20, _i(uvr5_agg if uvr5_agg not in (None, "") else agg, 10)))
  _tta = _b(uvr5_tta, _b(tta, _b(uvr5_enable_tta, False)))
  _pp = _b(uvr5_postprocess, _b(postprocess, _b(uvr5_enable_postprocess, False)))
  _win = _i(uvr5_window_size if uvr5_window_size not in (None, "") else
            (uvr5_win if uvr5_win not in (None, "") else
             (window_size if window_size not in (None, "") else 512)), 512)
  _hop = _i(uvr5_hop_length if uvr5_hop_length not in (None, "") else
            (uvr5_hop if uvr5_hop not in (None, "") else 128), 128)
  _high = str(uvr5_high_end_process or uvr5_high_end or "mirroring").strip() or "mirroring"
  if _b(uvr5_denoise) or _b(denoise):
    print("ℹ️ 插件请求了降噪（uvr5_denoise）：当前分离链路不含降噪模型，已忽略")
  if (msst_model or _b(msst_use_tta) or _i(msst_batch_size, 0)
      or _i(msst_num_overlap, 0) or _b(msst_normalize)):
    print(f"ℹ️ 插件传了 MSST 参数（model={msst_model} tta={_b(msst_use_tta)} "
          f"batch={msst_batch_size} overlap={msst_num_overlap} norm={_b(msst_normalize)}）："
          f"本后端用 UVR5 分离，已忽略")
  if _b(vocal_postprocess):
    print("ℹ️ 插件请求人声后处理（vocal_postprocess）：已按等效的后处理链处理")
  shift_acc = _b(shift_accompaniment, True)
  ensure_pre_fun(uvr5_model, agg, tta=_tta, postprocess=_pp,
                 window_size=_win, high_end_process=_high)

  if not song_name_src: raise gr.Error("请输入歌曲ID或链接！")
  
  if song_name_src.startswith("http"):
    try: song_name_src = song_name_src.split('id=')[1].split('&')[0]
    except IndexError: raise gr.Error("无效的网易云链接格式！")
  
  song_name_src = song_name_src.strip()
  print(f"处理歌曲ID: {song_name_src}")

  # 升降调只归一化一次：人声与伴奏必须用同一个值，否则 |shift| 在 7~11 时会差一个八度
  key_shift = optimize_pitch_shift(int(key_shift))
  if key_shift != 0:
    print(f"🎚️ 实际升降调: {key_shift:+d} 半音")
  
  audio_rvc_path = os.path.join(SCRIPT_DIR, "audio_rvc.wav")
  vocal_cache_path = f"./output/{split_model}/{song_name_src}/vocal_{song_name_src}.wav_{agg}.wav"
  
  # === 修复：检查文件是否真实存在，而不只是检查目录 ===
  if os.path.isfile(vocal_cache_path):
    print("✅ 歌曲已缓存，跳过下载")
    audio, sr = librosa.load(vocal_cache_path, sr=44100, mono=True)
    soundfile.write(audio_rvc_path, audio, sr)
  else:
    print("📥 未找到缓存，开始下载和分离")
    audio_rvc, sr_src = librosa.load(wwy_downloader(song_name_src, split_model)[0], sr=44100, mono=True)
    soundfile.write(audio_rvc_path, audio_rvc, sr_src)
  _lap("人声准备")

  print("🎤 RVC 推理中...")
  # 伴奏变调不依赖人声推理：先把它甩到后台，等推理做完再取结果（见 _finish_inst_shift）
  _inst_shift = None
  if (not _acapella) and key_shift != 0 and abs(key_shift) != 12 and shift_acc:
    print(f"🎹 伴奏变调 {key_shift:+d} 半音已在后台并行处理")
    _inst_shift = _start_inst_shift(
        f"output/{split_model}/{song_name_src}/instrument_{song_name_src}.wav_{agg}.wav",
        song_name_src, key_shift)
  switch_model(model_dropdown)
  _lap("切换模型")
  _f0 = (str(f0_method or "rmvpe")).strip() or "rmvpe"
  print(f"⚙️ f0={_f0} 索引比例={index_rate} 保护={protect} 响度混合={rms_mix_rate}")
  _index_file = find_index(model_dropdown)
  _lap("匹配索引")
  _resp = requests.post(f"{RVC_API_BASE}/run/infer_convert", json={
    "data": [
      0,
      None,
      {"name": audio_rvc_path, "data": None, "is_file": True},
      key_shift,
      _f0,
      _index_file,
      float(index_rate),
      int(resample_sr or 0),
      float(rms_mix_rate),
      float(protect),
  ]})

  # gradio 推理出错时 HTTP 500 + {"error": null}，真实原因只在 WebUI 日志里。
  # 以前直接 response["data"] 会抛 KeyError，用户只看到一段听不懂的堆栈。
  _lap("推理请求")
  try:
    response = _resp.json()
  except ValueError:
    raise gr.Error("RVC WebUI 返回了非 JSON（HTTP %s），它可能已经崩溃。\n日志：%s"
                   % (_resp.status_code, _WEBUI_LOG))

  if not isinstance(response, dict) or not response.get("data"):
    hint = _webui_tail_error()
    raise gr.Error("RVC 推理失败%s\n\n日志：%s\n常见原因：\n"
                   "  1. 基础模型配置不匹配（hubert 配置与权重卷积核不一致，重跑一键安装.bat 可修）\n"
                   "  2. 显卡显存不足（换短一点的歌，或关掉其它占显存的程序）\n"
                   "  3. 声线与当前 RVC 版本不兼容（换一个模型试试）"
                   % (("：" + hint) if hint else "（WebUI 没给出具体原因）", _WEBUI_LOG))

  data = response["data"][1]["name"]
  print(response["data"][0])
  _lap("RVC 推理")

  if data:
    print("🎛️ 开始处理音频")
    os.makedirs("./temp", exist_ok=True)

    audio_data, sr = librosa.load(data, sr=None, mono=False)

    if audio_data.ndim == 1:
        audio_data = audio_data.reshape(1, -1)
    # 单声道人声复制成立体声，否则 Pedalboard 的 Reverb width 不生效、导出也会变单声道
    if audio_data.shape[0] == 1:
        audio_data = np.repeat(audio_data, 2, axis=0)

    from pedalboard import Pedalboard, Compressor, Reverb, HighpassFilter, PeakFilter, LowpassFilter, PitchShift, Delay

    # ========== 修正后的智能混响参数计算 ==========
    # 定义参数的锚点
    # 强度级别:   0 (最小)       4 (默认)       10 (最大)
    room_size_map =  (0.15,          0.40,          0.90)
    wet_level_map =  (0.10,          0.25,          0.45)

    # 根据滑块位置，在两段之间进行线性插值
    if reverb_intensity <= 4:
        # 在 0-4 区间
        # 计算当前位置在该区间的百分比
        percent = reverb_intensity / 4.0
        # 在 (最小) 和 (默认) 参数之间插值
        room_size_val = room_size_map[0] + (room_size_map[1] - room_size_map[0]) * percent
        wet_level_val = wet_level_map[0] + (wet_level_map[1] - wet_level_map[0]) * percent
    else:
        # 在 4-10 区间
        # 计算当前位置在该区间的百分比
        percent = (reverb_intensity - 4) / 6.0  # (10 - 4 = 6)
        # 在 (默认) 和 (最大) 参数之间插值
        room_size_val = room_size_map[1] + (room_size_map[2] - room_size_map[1]) * percent
        wet_level_val = wet_level_map[1] + (wet_level_map[2] - wet_level_map[1]) * percent

    # 干信号总是与湿信号互补
    dry_level_val = 1.0 - wet_level_val

    print(f"🎤 混响设置: 强度 {reverb_intensity:g}/10 => 房间大小={room_size_val:.2f}, 湿润度={wet_level_val:.2f}")
    # ========================================
    inst_path = f"output/{split_model}/{song_name_src}/instrument_{song_name_src}.wav_{agg}.wav"    
    effects = [
        HighpassFilter(cutoff_frequency_hz=80),
        PeakFilter(cutoff_frequency_hz=200, gain_db=1.5, q=0.7),
        PeakFilter(cutoff_frequency_hz=3000, gain_db=2.0, q=1.0),
        PeakFilter(cutoff_frequency_hz=7000, gain_db=-3.0, q=2.0),
        LowpassFilter(cutoff_frequency_hz=16000),
        Compressor(
            threshold_db=-18.0,
            ratio=4.0,
            attack_ms=5.0,
            release_ms=150.0
        ),
    ]
    
    # ========== 只有当用户开启延迟时，才执行所有相关计算 ==========
    if delay_intensity > 0:
        print("🎤 启用回声效果，开始准备参数...")
        
        # 1. 自动检测歌曲BPM
        try:
            print("🎵 正在检测歌曲BPM...")
            y_inst, sr_inst = librosa.load(inst_path, sr=None)
            tempo, _ = librosa.beat.beat_track(y=y_inst, sr=sr_inst)
            
            # ========== 新增的健壮性检查 ==========
            # 检查 tempo 是否为 NumPy 数组，如果是，则提取其第一个元素
            # 这可以兼容返回单个浮点数或单元素数组的各种 librosa 版本
            if isinstance(tempo, np.ndarray):
                actual_tempo = tempo[0]
            else:
                actual_tempo = tempo
            # ========================================

            if actual_tempo > 0:
                # 现在使用 actual_tempo 进行所有操作
                print(f"✅ 检测到歌曲BPM约为: {actual_tempo:.1f}")
                delay_seconds_val = (60.0 / actual_tempo) * 0.5 
            else:
                print("⚠️ 未能检测到有效的BPM，将使用默认值。")
                delay_seconds_val = 0.5
                
        except Exception as e:
            # 打印具体的错误信息，方便未来调试
            print(f"⚠️ BPM检测失败: {type(e).__name__}: {e}，将使用默认值。")
            delay_seconds_val = 0.5
            
        # 2. 计算延迟混合度
        delay_mix_val = (delay_intensity / 10.0) * 0.35
        
        # 3. 将 Delay 效果器添加到列表中
        print(f"🎤 回声设置: 强度 {delay_intensity:g}/10 => 混合度={delay_mix_val:.2f}, 延迟时间={delay_seconds_val:.3f}s (BPM同步)")
        effects.append(
            Delay(
                delay_seconds=delay_seconds_val,
                feedback=0.25,
                mix=delay_mix_val
            )
        )
    # ==========================================================
    
    # 最后添加混响效果器 (这总是在延迟之后)
    effects.append(
        Reverb(
            room_size=room_size_val,
            damping=0.4,
            wet_level=wet_level_val,
            dry_level=dry_level_val,
            width=0.8
        )
    )
    
    # 用最终的效果器列表创建 Pedalboard
    board = Pedalboard(effects)

    processed = board(audio_data, sr)
    processed_int16 = (processed.T * 32768).astype(np.int16)
    processed_audio = AudioSegment(
        processed_int16.tobytes(),
        frame_rate=sr,
        sample_width=2,
        channels=processed.shape[0]
    )
    
    audio_vocal_adjusted = processed_audio + vocal_vol
    normalized_audio = pydub_normalize(audio_vocal_adjusted, headroom=-1.0) if normalize else audio_vocal_adjusted
    
    # ========== 处理伴奏音高 ==========
    print("🎵 准备伴奏...")
    
    # key_shift 已在函数开头归一化，这里直接使用，避免二次折叠
    # 当升降调不为0且不是±12（八度）时，同步调整伴奏
    if _acapella:
        print("🎤 清唱模式：完全跳过伴奏")
        audio_inst = None
    elif _inst_shift is not None:
        # 推理之前就甩到后台了，这里基本只是「取结果」
        audio_inst = _finish_inst_shift(_inst_shift)
    elif key_shift != 0 and abs(key_shift) != 12 and shift_acc:
        print(f"🎹 正在将伴奏音高调整 {key_shift:+d} 半音以匹配人声...")
        
        try:
            # 加载伴奏
            y_inst, sr_inst = librosa.load(inst_path, sr=None)
            
            # 创建一个只包含音高调整效果的 Pedalboard
            pitch_board = Pedalboard([
                PitchShift(semitones=key_shift)
            ])
            
            # 应用效果
            y_shifted = pitch_board(y_inst, sr_inst)
            
            # 保存处理后的伴奏为临时文件
            shifted_inst_path = f"temp/shifted_{song_name_src}_inst.wav"
            soundfile.write(shifted_inst_path, y_shifted, sr_inst)
            
            # 从处理后的文件加载为 AudioSegment
            audio_inst = AudioSegment.from_file(shifted_inst_path, format="wav")
            
            print(f"✅ 伴奏音高调整完成")
        except Exception as e:
            print(f"⚠️ 伴奏音高调整失败，使用原始伴奏: {e}")
            audio_inst = AudioSegment.from_file(inst_path, format="wav")
    else:
        # 不需要调整伴奏（key_shift为0或±12）
        if key_shift == 0:
            print("🎹 不调整伴奏音高")
        else:
            print(f"🎹 升降调为±12（八度），无需调整伴奏音高")
        audio_inst = AudioSegment.from_file(inst_path, format="wav")

    if _acapella or audio_inst is None:
        # 清唱：人声本身就是最终成品，不用再叠任何东西
        combined_audio = normalized_audio
        print("🎤 清唱输出：仅人声，无伴奏")
    else:
        audio_inst = audio_inst + inst_vol

        # 叠加前先按最坏情况（人声与伴奏峰值同时出现）预留 headroom。
        # pydub 的 overlay 会在 0dBFS 处硬截断，事后再降增益救不回被削掉的波形。
        try:
            _pv = 10 ** (normalized_audio.max_dBFS / 20.0)
            _pi = 10 ** (audio_inst.max_dBFS / 20.0)
            _target = 10 ** (-0.9 / 20.0)
            _sum = _pv + _pi
            if _sum > _target:
                _g = 20.0 * math.log10(_target / _sum)
                print(f"🔉 人声+伴奏峰值合计 {20.0 * math.log10(_sum):.2f} dBFS，先各自降 {abs(_g):.2f} dB 预留 headroom")
                normalized_audio = normalized_audio.apply_gain(_g)
                audio_inst = audio_inst.apply_gain(_g)
        except Exception as e:
            print(f"⚠️ headroom 预判跳过: {e}")

        combined_audio = normalized_audio.overlay(audio_inst)

    # 兜底 + 响度归位：上一步是按最坏情况压的，实际峰值往往没顶到，
    # 结果会偏轻。这里把整体峰值重新对齐到 -1.0 dBFS（此时波形没被削过，提回来是安全的）。
    try:
        _peak = combined_audio.max_dBFS
        _target_peak = -1.0
        if abs(_peak - _target_peak) > 0.1:
            _gain = _target_peak - _peak
            print(f"🔉 混音峰值 {_peak:.2f} dBFS -> 调整 {_gain:+.2f} dB 到 {_target_peak} dBFS")
            combined_audio = combined_audio.apply_gain(_gain)
        else:
            print(f"🔉 混音峰值 {_peak:.2f} dBFS，正常")
    except Exception as e:
        print(f"⚠️ 峰值保护跳过: {e}")

    _lap("效果与混音")
    print("💾 导出最终文件...")
    output_path = f"temp/{sanitize_filename(song_name_src)}-RVC-AI翻唱.mp3"
    combined_audio.export(
        output_path,
        format="MP3",
        bitrate="192k"
    )
    
    if os.path.isfile(data):
      os.remove(data)
    
    _lap("导出 MP3")
    print("⏱️ 本首耗时 %.1f 秒：" % (time.time() - _t_start)
          + " · ".join("%s %.1f" % (n, d) for n, d in _laps))
    print(f"✅ 已导出: {output_path}")
    return output_path

# ======================================================================
# 语音换声：给「TTS 朗读（或任意干净人声）」套上自己的 RVC 声线
# 与 convert() 的区别：不做伴奏分离、不加混响/延迟、不叠伴奏，
# 只走 WebUI 的 infer_convert 单文件推理，再做轻 EQ + 归一化导出 mp3。
# ======================================================================
def convert_voice_file(audio_path, model_dropdown, f0_method="rmvpe",
                       index_rate="0.75", protect="0.33",
                       rms_mix_rate="0.0", resample_sr="0",
                       key_shift="0"):
  _mark_job()          # 告诉预热线程「有真活了，让路」
  def _f(v, d=0.0):
    try:
      return float(re.search(r"-?\d+(?:\.\d+)?", str(v)).group())
    except Exception:
      return float(d)

  def _i(v, d=0):
    return int(round(_f(v, d)))

  src = str(audio_path or "").strip()
  if not src or not os.path.isfile(src):
    raise gr.Error("音频文件不存在：%s" % src)
  src = os.path.abspath(src)
  if not src.lower().endswith((".mp3", ".wav", ".flac", ".m4a", ".ogg")):
    raise gr.Error("不支持的音频格式：%s" % os.path.basename(src))

  # 只取文件名，挡住 ../ 之类的路径穿越
  model = os.path.basename(str(model_dropdown or "").strip())
  if not model.lower().endswith(".pth"):
    raise gr.Error("请选择一个声音模型")

  _f0 = (str(f0_method or "rmvpe")).strip() or "rmvpe"
  _ir = min(1.0, max(0.0, _f(index_rate, 0.75)))
  _pr = min(0.5, max(0.0, _f(protect, 0.33)))
  _rms = min(1.0, max(0.0, _f(rms_mix_rate, 0.0)))
  _key = max(-24, min(24, _i(key_shift, 0)))

  os.makedirs("temp", exist_ok=True)
  base = sanitize_filename(os.path.splitext(os.path.basename(src))[0])[:60] or "voice"
  wav_in = os.path.abspath(os.path.join("temp", "voice_in_%s_%d.wav" % (base, int(time.time()))))

  # 统一成 44.1k 单声道 wav：mp3 解码差异不会带到推理里
  audio, _sr = librosa.load(src, sr=44100, mono=True)
  if audio is None or len(audio) < 2560:          # <0.06s 基本是空文件
    raise gr.Error("音频内容为空或过短")
  peak = float(np.abs(audio).max())
  if peak > 1e-6:
    audio = audio / peak * 0.92
  soundfile.write(wav_in, audio, 44100)
  print("🎙️ 语音换声：模型=%s f0=%s 索引=%.2f 保护=%.2f 时长=%.1fs"
        % (model, _f0, _ir, _pr, len(audio) / 44100.0))

  switch_model(model)
  t0 = time.time()
  resp = requests.post(f"{RVC_API_BASE}/run/infer_convert", json={
    "data": [
      0,
      None,
      {"name": wav_in, "data": None, "is_file": True},
      _key,
      _f0,
      find_index(model),
      _ir,
      _i(resample_sr, 0),
      _rms,
      _pr,
    ]}, timeout=1800).json()

  try:
    out_wav = resp["data"][1]["name"]
  except Exception:
    raise gr.Error("后端没有返回音频：%s" % str(resp)[:300])
  if not out_wav or not os.path.isfile(out_wav):
    raise gr.Error("换声失败，后端未产出文件")
  print("✅ 换声完成，推理耗时 %.1fs" % (time.time() - t0))

  # 语音后处理：去低频轰鸣 → 轻压缩（拉平音量）→ 峰值归一到 -1 dBFS
  seg = AudioSegment.from_file(out_wav).set_channels(1)
  try:
    seg = seg.high_pass_filter(70)
  except Exception:
    pass
  try:
    from pedalboard import Pedalboard, Compressor as _Comp
    arr = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0
    board = Pedalboard([_Comp(threshold_db=-20.0, ratio=2.5,
                              attack_ms=6.0, release_ms=140.0)])
    arr = np.clip(board(arr.reshape(1, -1), seg.frame_rate)[0], -1.0, 1.0)
    i16 = (arr * 32767.0).astype(np.int16)
    seg = AudioSegment(i16.tobytes(), frame_rate=seg.frame_rate,
                       sample_width=2, channels=1)
  except Exception as e:
    print(f"⚠️ 语音压缩跳过: {type(e).__name__}: {e}")
  try:
    seg = pydub_normalize(seg, headroom=-1.0)
  except Exception as e:
    print(f"⚠️ 归一化跳过: {e}")

  out_dir = os.path.join("output", "tts")
  os.makedirs(out_dir, exist_ok=True)
  out_path = os.path.abspath(os.path.join(
      out_dir, "%s-voice-%d.mp3" % (base, int(time.time()))))
  seg.export(out_path, format="MP3", bitrate="192k")
  print("💾 已导出: %s (%.1f 秒)" % (out_path, len(seg) / 1000.0))

  for p in (wav_in, out_wav):
    try:
      if p and os.path.isfile(p) and os.path.abspath(p) != os.path.abspath(out_path):
        os.remove(p)
    except OSError:
      pass
  return out_path


def sanitize_filename(filename):
    # 定义 Windows 禁止的字符： \ / : * ? " < > |
    # 使用正则表达式移除这些字符
    clean_name = re.sub(r'[\\/:*?"<>|]', '', filename)
    return clean_name

# ===== 缓存统计 / 清理（插件 /查看翻唱缓存、/清理翻唱缓存 会调这两个端点）=====
_CACHE_AREAS = (("output", "分离结果(output)"), ("temp", "临时文件(temp)"))

def _dir_stats(path):
  files, total = 0, 0
  if os.path.isdir(path):
    for root, _dirs, names in os.walk(path):
      for n in names:
        try:
          total += os.path.getsize(os.path.join(root, n))
          files += 1
        except OSError:
          pass
  return files, total

def cache_info():
  """返回 {total_files, total_bytes, areas:{名称:{files,bytes}}}"""
  areas, tf, tb = {}, 0, 0
  for key, label in _CACHE_AREAS:
    f, b = _dir_stats(os.path.join(SCRIPT_DIR, key))
    areas[label] = {"files": f, "bytes": b}
    tf += f
    tb += b
  print(f"📦 缓存信息: {tf} 个文件 / {tb / 1048576:.1f} MiB")
  return {"total_files": tf, "total_bytes": tb, "areas": areas}

def clear_cache(scope="all"):
  """清空分离结果与临时文件，返回 {deleted_files, freed_bytes, scope}"""
  deleted, freed = 0, 0
  scope = str(scope or "all").strip() or "all"
  for key, _label in _CACHE_AREAS:
    root_dir = os.path.join(SCRIPT_DIR, key)
    if not os.path.isdir(root_dir):
      continue
    for root, dirs, names in os.walk(root_dir, topdown=False):
      for n in names:
        p = os.path.join(root, n)
        try:
          sz = os.path.getsize(p)
          os.remove(p)
          deleted += 1
          freed += sz
        except OSError:
          pass
      for dn in dirs:
        try:
          os.rmdir(os.path.join(root, dn))
        except OSError:
          pass
  print(f"🧹 缓存已清理: 删除 {deleted} 个文件，释放 {freed / 1048576:.1f} MiB（scope={scope}）")
  return {"deleted_files": deleted, "freed_bytes": freed, "scope": scope}

def refresh_models():
    """刷新模型列表的回调函数"""
    models_list = show_model() or _local_models()   # WebUI 没就绪也能列出来
    if models_list:
        return gr.Dropdown(choices=models_list, value=models_list[0] if models_list else None)
    else:
        return gr.Dropdown(choices=["无可用模型"], value="无可用模型")

def switch_model(model_name):
    """切换模型的回调函数 - 返回状态信息"""
    if not model_name or model_name == "无可用模型":
        return "❌ 请先选择一个有效的模型"
    result = change_model(model_name)
    return result


# ======================================================================
# 启动后后台预热：把「第一首歌要付的冷启动账」提前付掉
#
# 实测（E:\RVC-Data，机械盘 + RTX 5060）：启动后第一首歌要额外等
#   · 分离模型现场加载 43 秒（import torch + AudioPre 建模型）
#   · WebUI 侧 rmvpe 首次加载 + CUDA graph 捕获 30~40 秒
# 这些只在「第一次」发生，但用户感知就是「第一首歌要两分钟」。
#
# 这里只做能安全并行的部分：模块导入 + 分离模型预载，放在服务就绪之后、
# 后台线程里跑，不拖慢启动。真实任务开始时立刻让路（见 _job_*）。
# 内存不够的机器（<1.2GB 可用）自动跳过——宁可第一首慢点，也别 OOM。
# ======================================================================
_jobs = {"last": 0.0}          # 最近一次真实任务开始的时间（时间戳，不计数，不会漏减）
_jobs_lock = _threading.Lock()
PREWARM_DELAY = float(os.environ.get("RVC_PREWARM_DELAY", "6"))


def _mark_job():
    with _jobs_lock:
        _jobs["last"] = time.time()


def _job_active(window=180.0):
    """3 分钟内跑过真实任务就认为「在忙」，预热让路。"""
    with _jobs_lock:
        return (time.time() - _jobs["last"]) < window


def _port_alive(port, host="127.0.0.1", timeout=0.4):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def prewarm(level=None):
    """预热。level: full/uvr/import/off/auto（默认读 RVC_PREWARM）。"""
    level = str(level or os.environ.get("RVC_PREWARM") or "auto").strip().lower()
    if level in ("0", "off", "none", "no"):
        print("⏭️ 已关闭预热（RVC_PREWARM=off）")
        return False
    if _job_active():
        print("⏭️ 有任务在跑，热身让路（下个空闲周期再说）")
        return False
    free = _avail_mem_mb()
    need = 1200 if level == "import" else 2600
    if 0 <= free < need:
        print("⏭️ 可用内存只有 %.0f MB（预热要 %d MB），跳过预热" % (free, need))
        return False

    t0 = time.time()
    try:
        _get_torch()                       # 约 10 秒的 torch 导入
        from uvr5.vr import AudioPre       # noqa: F401  连带 librosa 那条链
        print("🔥 预热：torch / 分离模块已就绪（%.1f 秒）" % (time.time() - t0))
    except Exception as e:
        print("⚠️ 预热导入失败（不影响使用）: %s" % e)
        return False
    if level == "import":
        return True

    if _job_active():
        print("⏭️ 有任务在跑，跳过分离模型预载")
        return True
    try:
        get_pre_fun()                      # 预载分离模型（十几秒，常驻几百 MB）
        print("🔥 预热完成，共 %.1f 秒（首首歌不用再等这段）" % (time.time() - t0))
        return True
    except Exception as e:
        print("⚠️ 预热分离模型失败（不影响使用）: %s" % e)
        return False


def _start_prewarm_thread():
    """服务就绪之后、且 WebUI 也起完之后，再动手预热。

    为什么不能一到点就热：预热要读几百 MB（torch 的 DLL + 分离模型权重），
    和 WebUI 启动抢同一块盘，会把「端口就绪」从 13 秒拖到 33 秒——
    用户看到的就是「启动更慢了」。等 WebUI 起来再热，两条时间线就不打架了。
    """
    def run():
        for _ in range(900):
            if _port_alive(3333):
                break
            time.sleep(0.2)
        else:
            return
        # 等 WebUI 就绪（最多 120 秒）；只有后端的环境就等 10 秒后照常预热
        wait_webui = time.time()
        while time.time() - wait_webui < 120:
            if _port_alive(7865):
                break
            time.sleep(0.5)
        time.sleep(max(0.0, PREWARM_DELAY))
        try:
            prewarm()
        except Exception as e:
            print("⚠️ 预热异常（忽略）: %s" % e)

    t = _threading.Thread(target=run, daemon=True, name="prewarm")
    t.start()
    return t


app = gr.Blocks()

with app:
  gr.Markdown("# <center>RVC一键翻唱、重磅更新！</center>")
  gr.Markdown("## 自动分离人声翻唱并合并，自动混音！</center>")
  
  with gr.Row():
    with gr.Column():
      # 模型选择区域
      with gr.Row():
        model_dropdown = gr.Dropdown(
          label="选择AI模型", 
          choices=[], 
          value=None,
          info="请先点击刷新加载模型列表"
        )
        refresh_btn = gr.Button("🔄 刷新", size="sm")
        switch_btn = gr.Button("✨ 切换模型", size="sm", variant="primary")
      with gr.Row(visible=False):  # 隐藏这个功能
          models_json = gr.JSON()
          get_models_btn = gr.Button("获取模型列表", visible=False)
          get_models_btn.click(show_model, outputs=models_json)
      # 模型状态显示
      with gr.Row():
        model_status = gr.Textbox(label="模型状态", value="请选择模型", interactive=False)
      
      with gr.Row():
        inp1 = gr.Textbox(label="请填写想要AI翻唱的网易云id或链接", placeholder="114514", info="直接填写网易云id或链接")
      
      with gr.Row():
        inp5 = gr.Slider(minimum=-12, maximum=12, value=0, step=1, label="歌曲人声升降调", info="默认为0，+2为升高2个key，以此类推")
        inp6 = gr.Slider(minimum=-3, maximum=3, value=0, step=0.5, label="调节人声音量，默认为0")
        inp7 = gr.Slider(minimum=-3, maximum=3, value=0, step=0.5, label="调节伴奏音量，默认为0")
      # ========== 新增：混响强度滑块 ==========
      with gr.Row():
        inp_reverb = gr.Slider(
            minimum=0, maximum=10, value=4, step=0.5,
            label="混响强度",
            info="0为干声，4为默认值，10为宏大混响"
        )
      # ========================================
        inp_delay = gr.Slider(
            minimum=0, maximum=10, value=0, step=0.5,
            label="回声(延迟)效果",
            info="0为关闭，数值越大回声越明显"
        )
      btn = gr.Button("一键开启AI翻唱之旅吧💕", variant="primary")
    
    with gr.Column():
      out = gr.Audio(label="AI歌手为您倾情演唱的歌曲🎶", type="filepath", interactive=False,)

  # 绑定事件
  refresh_btn.click(refresh_models, outputs=model_dropdown,api_name=None)
  # 打开页面时自动拉一次模型列表：面板现在是 WebUI/后端并行启动，
  # 3333 可能比 WebUI 先就绪，靠这个保证 WebUI 晚到后一开页面就能看到声线列表。
  app.load(refresh_models, outputs=model_dropdown, api_name=None)
  switch_btn.click(switch_model, inputs=model_dropdown, outputs=model_status)
  btn.click(convert, [inp1, inp5, inp6, inp7,model_dropdown, inp_reverb, inp_delay], out, api_name="None")
  api_model_name = gr.Textbox(visible=False)
  api_output = gr.Audio(visible=False)
  # ===== 兼容新版插件的隐藏参数组件（顺序必须与 convert() 形参一致）=====
  # 全部用 Textbox：插件 schema 里既有 int 也有 string（如 index_rate='0.75'），
  # Number/Slider 会做类型与边界预检，str 传进来会直接抛 TypeError，函数根本进不去。
  api_uvr5_agg = gr.Textbox(value="10", visible=False)
  api_uvr5_model = gr.Textbox(value="", visible=False)
  api_split_model = gr.Textbox(value="UVR-HP5", visible=False)
  api_f0_method = gr.Dropdown(choices=["rmvpe", "crepe", "harvest", "pm"], value="rmvpe", visible=False)
  api_index_rate = gr.Textbox(value="0.75", visible=False)
  api_protect = gr.Textbox(value="0.33", visible=False)
  api_rms_mix = gr.Textbox(value="0.25", visible=False)
  api_resample_sr = gr.Textbox(value="0", visible=False)
  api_filter_radius = gr.Textbox(value="3", visible=False)
  api_out_fmt = gr.Textbox(value="mp3", visible=False)
  api_uvr5_hop = gr.Textbox(value="128", visible=False)
  api_uvr5_win = gr.Textbox(value="512", visible=False)
  api_uvr5_tta = gr.Textbox(value="false", visible=False)
  api_uvr5_pp = gr.Textbox(value="false", visible=False)
  api_uvr5_dn = gr.Textbox(value="false", visible=False)
  api_normalize = gr.Textbox(value="true", visible=False)
  # ===== 别名组件：插件不同版本可能用另一套命名，全部接收（顺序与形参别名区一致）=====
  _alias_order = ["uvr5_tta", "uvr5_postprocess", "uvr5_denoise", "uvr5_hop", "uvr5_win",
                  "uvr5_sample_rate", "uvr5_high_end", "uvr5_format", "uvr5_high_end_process",
                  "msst_model", "msst_use_tta", "msst_batch_size",
                  "msst_num_overlap", "msst_normalize",
                  "vocal_postprocess", "shift_accompaniment",
                  "tta", "postprocess", "window_size", "agg", "denoise",
                  "f0_min", "f0_max", "crepe_hop_length", "speaker_id",
                  "pitch_shift", "song_id", "model_name", "model",
                  "reverb", "echo", "delay", "vocal_gain", "inst_gain", "sample_rate",
                  "acapella"]
  api_extra = {}
  for _n in _alias_order:
    # 一律 Textbox：别名可能是数字也可能是字符串（msst_model、uvr5_format 等），
    # 用 Number 时一旦传进来非数字文本就会在组件预检阶段炸掉。
    api_extra[_n] = gr.Textbox(value=None, visible=False)
  gr.Button("API Convert", visible=False).click(
      convert,
      # 必须把全部形参都绑上输入组件，否则插件按关键字传参会被判为“不是合法关键字”
      inputs=[inp1, inp5, inp6, inp7, api_model_name, inp_reverb, inp_delay,
              api_uvr5_agg, api_uvr5_model, api_split_model, api_f0_method,
              api_index_rate, api_protect, api_rms_mix, api_resample_sr,
              api_filter_radius, api_out_fmt, api_uvr5_hop, api_uvr5_win,
              api_uvr5_tta, api_uvr5_pp, api_uvr5_dn, api_normalize] +
             [api_extra[_n] for _n in _alias_order],
      outputs=[api_output],
      api_name="convert"  # 这个才是外部API要调用的端点
  )
  # ===== 语音换声端点：TTS 朗读（或任意干净人声）套自己的声线 =====
  api_voice_in = gr.Textbox(visible=False)
  api_voice_model = gr.Textbox(visible=False)
  api_voice_f0 = gr.Dropdown(choices=["rmvpe", "crepe", "harvest", "pm"],
                             value="rmvpe", visible=False)
  api_voice_index = gr.Textbox(value="0.75", visible=False)
  api_voice_protect = gr.Textbox(value="0.33", visible=False)
  api_voice_rms = gr.Textbox(value="0.0", visible=False)
  api_voice_sr = gr.Textbox(value="0", visible=False)
  api_voice_key = gr.Textbox(value="0", visible=False)
  api_voice_out = gr.Audio(visible=False)
  gr.Button("API Voice", visible=False).click(
      convert_voice_file,
      inputs=[api_voice_in, api_voice_model, api_voice_f0, api_voice_index,
              api_voice_protect, api_voice_rms, api_voice_sr, api_voice_key],
      outputs=[api_voice_out],
      api_name="convert_voice_file")
  # ===== 缓存端点：插件「查看翻唱缓存 / 清理翻唱缓存」会用到 =====
  api_cache_out = gr.JSON(visible=False)
  api_cache_scope = gr.Textbox(value="all", visible=False)
  gr.Button("Cache Info", visible=False).click(
      cache_info, inputs=None, outputs=api_cache_out, api_name="cache_info")
  gr.Button("Clear Cache", visible=False).click(
      clear_cache, inputs=[api_cache_scope], outputs=api_cache_out, api_name="clear_cache")
  gr.Markdown("### <center>注意❗：请不要生成会对个人以及组织造成侵害的内容，此程序仅供科研、学习及个人娱乐使用。</center>")
  gr.HTML('''
      <div class="footer">
                  <p>🌊🏞️🎶 - 江水东流急，滔滔无尽声。 明·顾璘
                  </p>
      </div>
  ''')


print("正在初始化并加载模型列表...")
# 本地扫目录（毫秒级），不再同步等 WebUI —— 它慢的时候要 10~150 秒才应答，
# 那段等待以前是直接算在「后端启动」里的。
initial_models = _local_models()
if not initial_models:
    # 扫不到（装的目录结构不一样）才退回问 WebUI，且这时只能同步等
    initial_models = show_model()
else:
    models = initial_models
if initial_models:
    print(f"成功加载 {len(initial_models)} 个模型（本地扫描）")
else:
    print("⚠️ 警告: 未能加载模型列表，请确保RVC服务正在运行")


def _sync_models_when_webui_ready():
    """后台线程：等 WebUI 起来后把模型/索引列表跟它对齐（索引路径以它为准）。"""
    for _ in range(900):          # 最多等 3 分钟
        if _port_alive(7865):
            break
        time.sleep(0.2)
    else:
        return
    time.sleep(1.0)
    try:
        got = show_model()
        if got:
            print(f"🔄 已与 WebUI 对齐模型列表：{len(got)} 个")
    except Exception as e:
        print(f"⚠️ 对齐模型列表失败（不影响使用）: {e}")


_threading.Thread(target=_sync_models_when_webui_ready, daemon=True,
                  name="model-list-sync").start()
_start_prewarm_thread()


app.queue(max_size=40, api_open=False)
# 只绑回环：这个接口没有任何鉴权，绑 0.0.0.0 等于把 GPU 和模型管理交给整个局域网
app.launch(server_name=os.environ.get("RVC_BIND", "127.0.0.1"),
           server_port=3333, share=False, show_error=True)
