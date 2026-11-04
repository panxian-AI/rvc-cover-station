import os,sys
import subprocess
parent_directory = os.path.dirname(os.path.abspath(__file__))
import gc
import logging,pdb
logger = logging.getLogger(__name__)

import librosa
import numpy as np
import soundfile as sf
import torch
from uvr5.lib.lib_v5 import nets_61968KB as Nets
from uvr5.lib.lib_v5 import spec_utils
from uvr5.lib.lib_v5.model_param_init import ModelParameters
from uvr5.lib.utils import inference


def _quiet(cmd):
    """静默跑外部命令（这里是 ffmpeg 转码）。

    原来用 os.system：在没有控制台的进程里（控制台以静默方式启动时就是这种），
    Windows 会给 cmd.exe 新分配一个黑窗口，用户面前就闪一下。
    """
    try:
        subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception:
        pass


class AudioPre:
    def __init__(self, agg, model_path, device, is_half, tta=False):
        self.model_path = model_path
        self.device = device
        self.data = {
            # Processing Options
            "postprocess": False,
            "tta": tta,
            # Constants
            "window_size": 512,
            "agg": agg,
            "high_end_process": "mirroring",
        }
        mp = ModelParameters("%s/lib/lib_v5/modelparams/4band_v2.json"%parent_directory)
        model = Nets.CascadedASPPNet(mp.param["bins"] * 2)
        cpk = torch.load(model_path, map_location="cpu")
        model.load_state_dict(cpk)
        model.eval()
        if is_half and device == 'cuda':
            model = model.half().to(device)
        else:
            model = model.to(device)

        self.mp = mp
        self.model = model

    def _path_audio_(
        self, music_file, ins_root=None, vocal_root=None, format="flac", is_hp3=False
    ):
        if ins_root is None and vocal_root is None:
            return "No save root."
        name = os.path.basename(music_file)
        if ins_root is not None:
            os.makedirs(ins_root, exist_ok=True)
        if vocal_root is not None:
            os.makedirs(vocal_root, exist_ok=True)
        X_wave, y_wave, X_spec_s, y_spec_s = {}, {}, {}, {}
        bands_n = len(self.mp.param["band"])
        # print(bands_n)
        for d in range(bands_n, 0, -1):
            bp = self.mp.param["band"][d]
            if d == bands_n:  # high-end band
                (
                    X_wave[d],
                    _,
                ) = librosa.core.load(
                    music_file,
                    sr=bp["sr"],
                    mono=False,
                    dtype=np.float32,
                    res_type=bp["res_type"],
                )
                if X_wave[d].ndim == 1:
                    X_wave[d] = np.asfortranarray([X_wave[d], X_wave[d]])
            else:  # lower bands
                X_wave[d] = librosa.core.resample(
                    X_wave[d + 1],
                    orig_sr=self.mp.param["band"][d + 1]["sr"],
                    target_sr=bp["sr"],
                    res_type=bp["res_type"],
                )
                # 上一级（更高采样率）的波形到这里已经没用了，立刻放掉。
                # 一首 5 分钟的歌光这几个频带的波形就有 170MB，留着只是白占。
                X_wave.pop(d + 1, None)
            # Stft of wave source
            X_spec_s[d] = spec_utils.wave_to_spectrogram_mt(
                X_wave[d],
                bp["hl"],
                bp["n_fft"],
                self.mp.param["mid_side"],
                self.mp.param["mid_side_b2"],
                self.mp.param["reverse"],
            )
            # pdb.set_trace()
            if d == bands_n and self.data["high_end_process"] != "none":
                input_high_end_h = (bp["n_fft"] // 2 - bp["crop_stop"]) + (
                    self.mp.param["pre_filter_stop"] - self.mp.param["pre_filter_start"]
                )
                # 必须是 copy：原来的写法是切片视图，会把整个最高频频谱一起钉在
                # 内存里，直到函数结束都释放不掉。
                input_high_end = X_spec_s[d][
                    :, bp["n_fft"] // 2 - input_high_end_h : bp["n_fft"] // 2, :
                ].copy()

        X_spec_m = spec_utils.combine_spectrograms(X_spec_s, self.mp)
        # 各频带频谱已经合成进 X_spec_m，原来那份（一首 5 分钟的歌约 500MB）可以走了
        X_wave.clear()
        X_spec_s.clear()
        gc.collect()
        aggresive_set = float(self.data["agg"] / 100)
        aggressiveness = {
            "value": aggresive_set,
            "split_bin": self.mp.param["band"][1]["crop_stop"],
        }
        with torch.no_grad():
            pred, X_mag, X_phase = inference(
                X_spec_m, self.device, self.model, aggressiveness, self.data
            )
        # Postprocess
        if self.data["postprocess"]:
            pred_inv = np.clip(X_mag - pred, 0, np.inf)
            pred = spec_utils.mask_silence(pred, pred_inv)
        y_spec_m = pred * X_phase
        v_spec_m = X_spec_m - y_spec_m
        # 人声/伴奏两套频谱已经算出来了，推理中间量统统放掉：
        # 这两个（y_spec_m 和 v_spec_m）各自就有一两百 MB。
        del pred, X_mag, X_phase, X_spec_m
        gc.collect()

        if is_hp3 == True:
            ins_root,vocal_root = vocal_root,ins_root

        if ins_root is not None:
            if self.data["high_end_process"].startswith("mirroring"):
                input_high_end_ = spec_utils.mirroring(
                    self.data["high_end_process"], y_spec_m, input_high_end, self.mp
                )
                wav_instrument = spec_utils.cmb_spectrogram_to_wave(
                    y_spec_m, self.mp, input_high_end_h, input_high_end_
                )
            else:
                wav_instrument = spec_utils.cmb_spectrogram_to_wave(y_spec_m, self.mp)
            logger.info("%s instruments done" % name)
            if is_hp3 == True:
                head = "vocal_"
            else:
                head = "instrument_"
            if format in ["wav", "flac"]:
                sf.write(
                    os.path.join(
                        ins_root,
                        head + "{}_{}.{}".format(name, self.data["agg"], format),
                    ),
                    (np.array(wav_instrument)).astype("float32"),
                    self.mp.param["sr"],
                )  #
            else:
                path = os.path.join(
                    ins_root, head + "{}_{}.wav".format(name, self.data["agg"])
                )
                sf.write(
                    path,
                    (np.array(wav_instrument)).astype("float32"),
                    self.mp.param["sr"],
                )
                if os.path.exists(path):
                    opt_format_path = path[:-4] + ".%s" % format
                    _quiet("ffmpeg -i %s -vn %s -q:a 2 -y" % (path, opt_format_path))
                    if os.path.exists(opt_format_path):
                        try:
                            os.remove(path)
                        except:
                            pass
        if vocal_root is not None:
            if ins_root is not None:
                # 伴奏已经写盘，合成人声时不必再拖着伴奏的整份频谱（~270MB）
                del y_spec_m
                gc.collect()
            if is_hp3 == True:
                head = "instrument_"
            else:
                head = "vocal_"
            if self.data["high_end_process"].startswith("mirroring"):
                input_high_end_ = spec_utils.mirroring(
                    self.data["high_end_process"], v_spec_m, input_high_end, self.mp
                )
                wav_vocals = spec_utils.cmb_spectrogram_to_wave(
                    v_spec_m, self.mp, input_high_end_h, input_high_end_
                )
            else:
                wav_vocals = spec_utils.cmb_spectrogram_to_wave(v_spec_m, self.mp)
            logger.info("%s vocals done" % name)
            if format in ["wav", "flac"]:
                sf.write(
                    os.path.join(
                        vocal_root,
                        head + "{}_{}.{}".format(name, self.data["agg"], format),
                    ),
                    (np.array(wav_vocals)).astype("float32"),
                    self.mp.param["sr"],
                )
            else:
                path = os.path.join(
                    vocal_root, head + "{}_{}.wav".format(name, self.data["agg"])
                )
                sf.write(
                    path,
                    (np.array(wav_vocals)).astype("float32"),
                    self.mp.param["sr"],
                )
                if os.path.exists(path):
                    opt_format_path = path[:-4] + ".%s" % format
                    _quiet("ffmpeg -i %s -vn %s -q:a 2 -y" % (path, opt_format_path))
                    if os.path.exists(opt_format_path):
                        try:
                            os.remove(path)
                        except:
                            pass
