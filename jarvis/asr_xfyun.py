"""讯飞「语音听写（流式版 IAT）」云端识别后端。

把一段 16k/单声道/float32 的音频，经 WebSocket 上传到讯飞，返回转写文本。
中文识别通常比本地 whisper-small 更准。任何失败都向上抛异常，由 asr.py 回退本地。

鉴权按讯飞 WebAPI 规范：用 APISecret 对 (host/date/request-line) 做 HMAC-SHA256，
再 base64 组装成 authorization，拼到 wss URL 的查询参数里。
需要依赖 websocket-client（仅启用讯飞时才用到）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time

import numpy as np

from . import config

_HOST = "iat-api.xfyun.cn"
_PATH = "/v2/iat"
_URL = f"wss://{_HOST}{_PATH}"


def _auth_url() -> str:
    """生成带鉴权参数的 wss URL。"""
    date = format_date_time(time.time())          # RFC1123 GMT 时间
    sign_origin = f"host: {_HOST}\ndate: {date}\nGET {_PATH} HTTP/1.1"
    sign = base64.b64encode(
        hmac.new(config.XFYUN_API_SECRET.encode("utf-8"),
                 sign_origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    auth_origin = (f'api_key="{config.XFYUN_API_KEY}", '
                   f'algorithm="hmac-sha256", '
                   f'headers="host date request-line", signature="{sign}"')
    authorization = base64.b64encode(auth_origin.encode("utf-8")).decode()
    return _URL + "?" + urlencode(
        {"authorization": authorization, "date": date, "host": _HOST})


def transcribe(audio: np.ndarray) -> str:
    """上传音频，返回识别文本（可能为空串表示没听清）。失败抛异常。"""
    import websocket  # 延迟导入：没装也不影响本地后端

    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    frame = 1280                                   # 40ms @ 16k/16bit/单声道
    chunks = [pcm[i:i + frame] for i in range(0, len(pcm), frame)] or [b""]

    ws = websocket.create_connection(_auth_url(), timeout=10)
    try:
        for i, ch in enumerate(chunks):
            status = 0 if i == 0 else 1            # 0=首帧 1=中间帧
            payload: dict = {
                "data": {
                    "status": status,
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(ch).decode(),
                }
            }
            if i == 0:
                payload["common"] = {"app_id": config.XFYUN_APP_ID}
                payload["business"] = {
                    "language": "zh_cn", "domain": "iat",
                    "accent": "mandarin", "vad_eos": 3000,
                }
            ws.send(json.dumps(payload))
            time.sleep(0.005)                      # 整段音频已录好，快速送即可（仅留极小间隔防限流）
        # 结束帧
        ws.send(json.dumps({"data": {
            "status": 2, "format": "audio/L16;rate=16000",
            "encoding": "raw", "audio": ""}}))

        text = ""
        while True:
            msg = ws.recv()
            if not msg:
                break
            obj = json.loads(msg)
            if obj.get("code") != 0:
                raise RuntimeError(
                    f"讯飞返回 {obj.get('code')}：{obj.get('message')}")
            data = obj.get("data") or {}
            for w in (data.get("result") or {}).get("ws", []):
                for c in w.get("cw", []):
                    text += c.get("w", "")
            if data.get("status") == 2:            # 最后一帧结果
                break
        return text.strip()
    finally:
        ws.close()
