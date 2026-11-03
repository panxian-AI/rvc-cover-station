# -*- coding: utf-8 -*-
"""RVC-WebUI / 翻唱后端 基础模型与工具一键下载。

仓库不含大文件（GitHub 单文件上限 100MB），本脚本补齐运行所需的模型与工具：
  - rmvpe.pt（音高提取，RVC 必需）          —— 已验证镜像，自动下载 + 字节数校验
  - 小肥鱼(FeiyuVoice) 声线 .pth + .index   —— 公开模型，已验证镜像
  - ffmpeg / ffprobe（BtbN 官方构建）       —— 自动下载解压
  - hubert 的 config（直接内置在脚本里）
其余大模型来源无法保证长期稳定，脚本只给校验和手动指引（也可 --url 覆盖）。

用法：
    python 下载基础模型.py             # 下载全部可自动获取的
    python 下载基础模型.py --url rmvpe=https://xxx/rmvpe.pt
    python 下载基础模型.py --only rmvpe,ffmpeg
默认假设脚本上级目录与 RVC-WebUI/、RVCSVC-API/ 平级（即仓库布局）。
"""
import argparse
import json
import os
import shutil
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 布局自适应：
#   仓库布局  RVC-WebUI/ 与 setup/ 同级（git clone / zip 解压后）
#   运行布局  RVC-WebUI/ 在 RVCSVC 的上级旁（一键安装.bat 装出来的样子）
_W1 = os.path.join(ROOT, "RVC-WebUI")
if os.path.isdir(_W1):
    WEBUI = _W1
else:
    WEBUI = os.path.join(os.path.dirname(ROOT), "RVC-WebUI")
API = os.path.join(os.path.dirname(WEBUI), "RVCSVC-API") if os.path.isdir(
    os.path.join(ROOT, "RVC-WebUI")) else os.path.join(ROOT, "RVCSVC-API")

# GitHub 加速前缀（可自定义下载地址）：set RVC_GH_PREFIX=https://ghfast.top/
GH_PREFIX = os.environ.get("RVC_GH_PREFIX", "").strip()

# 已验证可用的镜像（2026-11-03 实测 200 + 字节数一致）
ITEMS = {
    "rmvpe": {
        "desc": "rmvpe.pt 音高提取模型",
        "dest": os.path.join(WEBUI, "assets", "rmvpe", "rmvpe.pt"),
        "size": 181184272,
        "urls": ["https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt"],
    },
    "feiyu_pth": {
        "desc": "小肥鱼声线 feiyu.pth",
        "dest": os.path.join(WEBUI, "assets", "weights", "feiyu.pth"),
        "size": 55191150,
        "urls": ["https://hf-mirror.com/Esmerubphire/FeiyuVoice-RVC/resolve/main/FeiyuVoice-RVC-v1.pth"],
    },
    "feiyu_index": {
        "desc": "小肥鱼声线索引 feiyu_added_v2.index",
        "dest": os.path.join(WEBUI, "assets", "indices", "feiyu_added_v2.index"),
        "size": 32004419,
        "urls": ["https://hf-mirror.com/Esmerubphire/FeiyuVoice-RVC/resolve/main/FeiyuVoice-RVC-v1.index"],
    },
}

FFMPEG_URL = GH_PREFIX + "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"

# hubert 的 transformers 配置直接内置（本体 pytorch_model.bin 见 MANUAL）
#
# conv_kernel 必须与权重逐层对齐：本项目的 hubert_base 权重（fairseq hubert_base
# 转 transformers 格式）最后两层卷积核是 2，前五层是 3 —— 写错会得到
#   RuntimeError: size mismatch for feature_extractor.conv_layers.5.conv.weight
# 而 RVC 的推理接口只会返回 HTTP 500 + {"error": null}，用户根本看不出原因。
HUBERT_CONV_KERNEL = [10, 3, 3, 3, 3, 2, 2]
HUBERT_CONFIG = {
    "config.json": r'''{
  "activation_dropout": 0.0,
  "apply_spec_augment": true,
  "architectures": ["HubertModelWithFinalProj"],
  "attention_dropout": 0.1,
  "bos_token_id": 1,
  "classifier_proj_size": 256,
  "conv_bias": false,
  "conv_dim": [512, 512, 512, 512, 512, 512, 512],
  "conv_kernel": [10, 3, 3, 3, 3, 2, 2],
  "conv_stride": [5, 2, 2, 2, 2, 2, 2],
  "do_stable_layer_norm": false,
  "feat_extract_activation": "gelu",
  "feat_extract_dropout": 0.0,
  "feat_extract_norm": "group",
  "feat_proj_dropout": 0.1,
  "feat_proj_layer_norm": true,
  "final_dropout": 0.1,
  "gradient_checkpointing": false,
  "hidden_act": "gelu",
  "hidden_dropout": 0.1,
  "hidden_dropout_prob": 0.1,
  "hidden_size": 768,
  "initializer_range": 0.02,
  "intermediate_size": 3072,
  "layer_norm_eps": 1e-05,
  "layerdrop": 0.1,
  "mask_feature_length": 10,
  "mask_feature_min_masks": 0,
  "mask_feature_prob": 0.0,
  "mask_time_length": 10,
  "mask_time_min_masks": 2,
  "mask_time_prob": 0.05,
  "model_type": "hubert",
  "num_attention_heads": 12,
  "num_conv_pos_embedding_groups": 16,
  "num_conv_pos_embeddings": 128,
  "num_hidden_layers": 12,
  "pad_token_id": 0,
  "torch_dtype": "float16",
  "transformers_version": "4.31.0.dev0",
  "use_weighted_layer_sum": false,
  "vocab_size": 32
}''',
    "preprocessor_config.json": r'''{
  "do_normalize": false,
  "feature_extractor_type": "Wav2Vec2FeatureExtractor",
  "feature_size": 1,
  "padding_side": "right",
  "padding_value": 0.0,
  "return_attention_mask": false,
  "sampling_rate": 16000
}''',
}

MANUAL = {
    "hubert": {
        "desc": "hubert 本体 RVC-WebUI/assets/hubert_base/pytorch_model.bin",
        "size": 189206711,
        "hint": "HubertModelWithFinalProj 格式。从你已有的 RVC 安装复制，或在 "
                "huggingface.co 搜 HubertModelWithFinalProj；拿到后也可 "
                "--url hubert=<直链> 让脚本自动下。",
    },
    "roformer": {
        "desc": "人声分离模型 RVC-WebUI/assets/pymss_weights/model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "size": 319821424,
        "hint": "MSST/UVR 社区的 bs-roformer karaoke 模型，从 MSST-WebUI 项目发布页获取；"
                "同目录的 *.yaml 已随仓库附带。",
    },
    "uvr5_hp5": {
        "desc": "翻唱后端分离模型 RVCSVC-API/uvr5/uvr_model/5_HP-Karaoke-UVR.pth",
        "size": None,
        "hint": "UVR5 官方模型包（5_HP-Karaoke-UVR），从 UVR 项目发布页获取后放进该目录。",
    },
    "voices": {
        "desc": "其它声线 assets/weights/*.pth + assets/indices/*.index",
        "size": None,
        "hint": "自己训练（WebUI 训练页）或从模型作者处获取，文件名即声线名。",
    },
}


def repair_hubert_config(hub):
    """把 hubert 的 config.json 纠正到与权重一致（幂等；改过返回 True）。

    hubert_base 权重的卷积核是 [10,3,3,3,3,2,2]。历史版本这里写成了
    [10,3,3,3,3,3,3]，transformers 加载时报
    size mismatch for feature_extractor.conv_layers.5.conv.weight，
    而 RVC 推理接口只回 HTTP 500 + {"error": null}，界面上什么线索都没有。
    """
    p = os.path.join(hub, "config.json")
    if not os.path.isfile(p):
        return False
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return False
    if cfg.get("conv_kernel") == HUBERT_CONV_KERNEL:
        return False
    try:
        shutil.copyfile(p, p + ".bak")
    except Exception:
        pass
    cfg["conv_kernel"] = HUBERT_CONV_KERNEL
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("  ! 已修正 hubert config.json 的 conv_kernel → %s" % HUBERT_CONV_KERNEL)
    return True


def human(n):
    return "%.1f MB" % (n / 1048576) if n else "?"


def fetch(url, dest, size, label):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    print("  ↓ %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0) or (size or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                sys.stdout.write("    %d%%  %s/%s\r" % (done * 100 // total,
                                                        human(done), human(total)))
                sys.stdout.flush()
    print()
    if size and os.path.getsize(tmp) != size:
        os.remove(tmp)
        print("  ✗ 字节数不符（期望 %s）——已丢弃，请换源" % human(size))
        return False
    os.replace(tmp, dest)
    print("  ✓ 已保存 %s (%s)" % (os.path.relpath(dest, ROOT), human(os.path.getsize(dest))))
    return True


def ensure_ffmpeg():
    ff = os.path.join(WEBUI, "ffmpeg.exe")
    fp = os.path.join(WEBUI, "ffprobe.exe")
    if os.path.isfile(ff) and os.path.isfile(fp):
        print("✓ ffmpeg / ffprobe 已存在")
        return True
    print("== ffmpeg / ffprobe ==")
    z = os.path.join(HERE, "_ffmpeg.zip")
    try:
        print("  ↓ " + FFMPEG_URL)
        urllib.request.urlretrieve(FFMPEG_URL, z)
        with zipfile.ZipFile(z) as f:
            for info in f.infolist():
                base = os.path.basename(info.filename)
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    with open(os.path.join(WEBUI, base), "wb") as out, f.open(info) as src:
                        out.write(src.read())
        os.remove(z)
        print("  ✓ 已解出 ffmpeg.exe / ffprobe.exe 到 RVC-WebUI/")
        return True
    except Exception as e:
        if os.path.isfile(z):
            os.remove(z)
        print("  ✗ 下载失败：%s: %s" % (type(e).__name__, e))
        print("    手动：从 " + FFMPEG_URL + " 取 bin 目录下两个 exe")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[],
                    help="覆盖下载地址，如 --url rmvpe=https://.../rmvpe.pt")
    ap.add_argument("--only", default="", help="只处理这些项，逗号分隔")
    a = ap.parse_args()
    overrides = {}
    for kv in a.url:
        k, _, v = kv.partition("=")
        overrides[k.strip()] = v.strip()

    ok = bad = 0
    for key, it in ITEMS.items():
        if a.only and key not in a.only.split(","):
            continue
        print("== %s (%s) ==" % (key, it["desc"]))
        dest = it["dest"]
        if os.path.isfile(dest) and (not it["size"] or os.path.getsize(dest) == it["size"]):
            print("  ✓ 已存在，跳过")
            ok += 1
            continue
        url = overrides.get(key) or (it["urls"][0] if it["urls"] else None)
        if url and fetch(url, dest, it["size"], key):
            ok += 1
        else:
            bad += 1

    # hubert 配置（小文件直接内置写出）
    hub = os.path.join(WEBUI, "assets", "hubert_base")
    os.makedirs(hub, exist_ok=True)
    for name, content in HUBERT_CONFIG.items():
        p = os.path.join(hub, name)
        if not os.path.isfile(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
    # 老安装可能带着写错的 conv_kernel（历史 bug：最后两层写成 3，权重是 2）。
    # 这里主动纠正，否则重跑安装器也修不好，推理一律 500。
    repair_hubert_config(hub)
    binf = os.path.join(hub, "pytorch_model.bin")
    if os.path.isfile(binf) and os.path.getsize(binf) == MANUAL["hubert"]["size"]:
        print("✓ hubert 本体已就位")
    else:
        hub_url = overrides.get("hubert")
        if hub_url:
            print("== hubert 本体（使用 --url 覆盖地址）==")
            if fetch(hub_url, binf, MANUAL["hubert"]["size"], "hubert"):
                ok += 1
            else:
                bad += 1
        else:
            print("== hubert 本体需要手动获取 ==")
            print("  " + MANUAL["hubert"]["hint"])

    if not a.only or "ffmpeg" in a.only.split(","):
        if ensure_ffmpeg():
            ok += 1
        else:
            bad += 1

    print("\n===== 还需手动准备的大模型 =====")
    for k, m in MANUAL.items():
        print("· %s\n    %s" % (m["desc"], m["hint"]))
    print("\n完成：自动项 %d 成功 / %d 失败" % (ok, bad))


if __name__ == "__main__":
    main()
