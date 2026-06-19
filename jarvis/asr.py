"""语音识别。

两个后端（config.ASR_BACKEND 切换）：
  - local：本地 faster-whisper（离线、免费、隐私，声音不出本机）
  - xfyun：讯飞云端「语音听写」（中文更准；需联网、会上传音频）
讯飞失败/超时时自动回退本地，保证不哑火。
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
from faster_whisper import WhisperModel

from . import config

_model: WhisperModel | None = None
_xfyun_warned = False          # 讯飞回退只提示一次，避免刷屏


class ASRResult(NamedTuple):
    text: str
    no_speech: float       # 越大越像噪音/无语音
    avg_logprob: float     # 越大越可信


def load() -> None:
    """加载本地模型（首次运行会自动下载，存到 ~/.cache）。

    即便用讯飞后端也照样加载，作为云端连不上时的兜底。"""
    global _model
    if _model is None:
        _model = WhisperModel(
            config.WHISPER_MODEL,
            device="cpu",
            compute_type=config.WHISPER_COMPUTE,
        )


def transcribe(audio: np.ndarray, cloud: bool = False) -> ASRResult:
    """把一段音频转成中文文本，并附带置信度信息。

    cloud=True 且后端为 xfyun 时走讯飞云端（更准，但会上传+计费）；否则一律本地。
    省配额策略：待机听唤醒词用本地(cloud=False)，唤醒后的命令才用讯飞(cloud=True)。
    """
    if cloud and config.ASR_BACKEND == "xfyun":
        global _xfyun_warned
        try:
            from . import asr_xfyun
            text = asr_xfyun.transcribe(audio)
            # 云端有结果就视为可信（no_speech 低、logprob 高，能过唤醒过滤）；
            # 空串表示云端 VAD 判为没说话，按「没听到」处理，不回退本地。
            return ASRResult(text, 0.0, 0.0)
        except Exception as e:  # noqa: BLE001 云端异常 → 回退本地
            if not _xfyun_warned:
                print(f"  ⚠ 讯飞识别失败，本次起回退本地 whisper：{e}")
                _xfyun_warned = True
    return _transcribe_local(audio)


def _transcribe_local(audio: np.ndarray) -> ASRResult:
    """本地 faster-whisper 识别。"""
    if _model is None:
        load()
    assert _model is not None
    segments, _ = _model.transcribe(
        audio,
        language=config.ASR_LANGUAGE,
        beam_size=config.ASR_BEAM,             # 精度/速度旋钮（config.ASR_BEAM）
        vad_filter=config.ASR_VAD,             # 再过滤一遍静音，减少噪音幻听
        initial_prompt=config.ASR_INITIAL_PROMPT,  # 简体定调 + 可塞常用词/人名
        condition_on_previous_text=False,      # 不带上文，避免额外开销与重复漂移
    )
    segs = list(segments)
    text = "".join(s.text for s in segs).strip()
    if not segs:
        return ASRResult("", 1.0, -10.0)
    no_speech = sum(s.no_speech_prob for s in segs) / len(segs)
    avg_logprob = sum(s.avg_logprob for s in segs) / len(segs)
    return ASRResult(text, no_speech, avg_logprob)
