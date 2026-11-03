import json
import logging
from functools import lru_cache
from pathlib import Path

import torch
from torch import nn
from transformers import AutoFeatureExtractor, HubertModel

from tools.cuda_graph import run_cuda_graph


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HubertModelWithFinalProj(HubertModel):
    def __init__(self, config):
        super().__init__(config)
        self.final_proj = nn.Linear(config.hidden_size, config.classifier_proj_size)

HUBERT_MODEL_PATH = (PROJECT_ROOT / "assets" / "hubert_base").resolve()


def _device_type(device):
    if isinstance(device, torch.device):
        return device.type
    return str(device).split(":", 1)[0]


# 权重文件里卷积层的实际形状才是真相；config.json 是手写的，容易与权重不一致。
# 历史 bug：conv_kernel 写成 [10,3,3,3,3,3,3]，权重是 [10,3,3,3,3,2,2]，
# 加载时报 size mismatch，而 RVC 推理接口只回 HTTP 500 + {"error": null}。
_WEIGHT_FILES = ("pytorch_model.bin", "model.safetensors")


def _conv_shapes_from_weights():
    """读出权重里 feature_extractor 各层卷积的 (out_dim, kernel)，读不到返回 None。"""
    bin_path = HUBERT_MODEL_PATH / "pytorch_model.bin"
    state = None
    if bin_path.is_file():
        try:
            state = torch.load(str(bin_path), map_location="cpu", weights_only=True)
        except Exception as e:  # 老 torch / 非 zip 格式
            logger.warning("读取 hubert 权重失败，跳过配置自检: %s", e)
            return None
    else:
        try:
            from safetensors.torch import load_file

            sf = HUBERT_MODEL_PATH / "model.safetensors"
            if not sf.is_file():
                return None
            state = load_file(str(sf))
        except Exception:
            return None

    dims, kernels = [], []
    idx = 0
    while True:
        key = f"feature_extractor.conv_layers.{idx}.conv.weight"
        if key not in state:
            break
        shape = tuple(state[key].shape)
        dims.append(shape[0])
        kernels.append(shape[2])
        idx += 1
    return (dims, kernels) if kernels else None


def repair_config_from_weights():
    """把 config.json 的 conv_dim/conv_kernel 校正到与权重一致。改过返回 True。"""
    shapes = _conv_shapes_from_weights()
    if not shapes:
        return False
    dims, kernels = shapes
    cfg_path = HUBERT_MODEL_PATH / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if cfg.get("conv_dim") == dims and cfg.get("conv_kernel") == kernels:
        return False
    try:
        cfg_path.with_suffix(".json.bak").write_text(
            cfg_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except Exception:
        pass
    cfg["conv_dim"] = dims
    cfg["conv_kernel"] = kernels
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.warning(
        "hubert config.json 与权重不一致，已自动修正 conv_dim=%s conv_kernel=%s",
        dims,
        kernels,
    )
    return True


def load_hubert_model(device, is_half=False):
    """Load the local Transformers HuBERT/ContentVec model for RVC."""
    if not (HUBERT_MODEL_PATH / "config.json").is_file():
        raise FileNotFoundError(
            f"Transformers HuBERT model not found: {HUBERT_MODEL_PATH}"
        )

    dtype = torch.float16 if is_half else torch.float32
    load_options = {
        "local_files_only": True,
        "torch_dtype": dtype,
    }
    # DirectML does not implement every SDPA kernel used by Transformers.
    if _device_type(device) == "privateuseone":
        load_options["attn_implementation"] = "eager"

    logger.info(
        "Loading Transformers HuBERT from %s (%s on %s)",
        HUBERT_MODEL_PATH,
        dtype,
        device,
    )
    try:
        model = HubertModelWithFinalProj.from_pretrained(
            str(HUBERT_MODEL_PATH), **load_options
        )
    except RuntimeError as e:
        # 只处理「配置写错」这一类，别的错误（显存、文件损坏）原样抛出
        if "size mismatch" not in str(e) or not repair_config_from_weights():
            raise
        model = HubertModelWithFinalProj.from_pretrained(
            str(HUBERT_MODEL_PATH), **load_options
        )
    model = model.to(device)
    return model.eval()


@lru_cache(maxsize=1)
def hubert_audio_requires_normalization():
    feature_extractor = AutoFeatureExtractor.from_pretrained(
        str(HUBERT_MODEL_PATH), local_files_only=True
    )
    return bool(feature_extractor.do_normalize)


def extract_hubert_features(model, source, version, padding_mask=None):
    """Return the RVC v1 (256-D) or v2 (768-D) HuBERT representation.

    Transformers hidden_states[N] is numerically equivalent to the source checkpoint's
    output_layer=N for this converted checkpoint. RVC v1 uses layer 9 followed
    by final_proj; RVC v2 uses the final (12th) encoder layer directly.
    """
    if version not in {"v1", "v2"}:
        raise ValueError(f"Unsupported RVC feature version: {version!r}")

    attention_mask = None
    if padding_mask is not None and bool(torch.any(padding_mask).item()):
        attention_mask = (~padding_mask.bool()).long()

    if version == "v1":
        if attention_mask is None:
            def forward(input_values):
                outputs = model(
                    input_values=input_values,
                    attention_mask=None,
                    output_hidden_states=True,
                    return_dict=True,
                )
                return model.final_proj(outputs.hidden_states[9])

            return run_cuda_graph(model, "hubert-v1-no-mask", forward, source)

        def forward(input_values, mask):
            outputs = model(
                input_values=input_values,
                attention_mask=mask,
                output_hidden_states=True,
                return_dict=True,
            )
            return model.final_proj(outputs.hidden_states[9])

        return run_cuda_graph(
            model, "hubert-v1-mask", forward, source, attention_mask
        )

    if attention_mask is None:
        def forward(input_values):
            return model(
                input_values=input_values,
                attention_mask=None,
                output_hidden_states=False,
                return_dict=True,
            ).last_hidden_state

        return run_cuda_graph(model, "hubert-v2-no-mask", forward, source)

    def forward(input_values, mask):
        return model(
            input_values=input_values,
            attention_mask=mask,
            output_hidden_states=False,
            return_dict=True,
        ).last_hidden_state

    return run_cuda_graph(model, "hubert-v2-mask", forward, source, attention_mask)
