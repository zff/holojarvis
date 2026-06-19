"""贾维斯主程序：把麦克风、识别、唤醒、大脑、朗读串成一个循环。

运行：
    python -m jarvis            # 带桌面发光球桌宠（默认）
    python -m jarvis --no-pet   # 纯命令行，不弹窗

状态机：
    待机(idle) —— 一直听，直到听到唤醒词「贾维斯」（或点一下桌宠）
    清醒(active) —— 听到的每句话都交给 Claude 处理；静默一段时间后回到待机
桌宠(GUI)跑主线程，语音助手跑后台线程，经线程安全接口通信。
"""

from __future__ import annotations

import difflib
import queue
import re
import subprocess
import sys
import threading
import time

from pypinyin import lazy_pinyin

from . import asr, config, tts
from .audio import Microphone
from .brain import Brain

_PUNCT = " ，。！？、,.!?～~"


# ---- 唤醒词：拼音模糊匹配 --------------------------------------------

def _phon(text: str) -> str:
    """转成"模糊拼音"：去声调、合并 zh/ch/sh→z/c/s、前后鼻音等，便于按发音匹配。"""
    py = "".join(lazy_pinyin(text))
    py = re.sub(r"[^a-z]", "", py.lower())
    py = py.replace("zh", "z").replace("ch", "c").replace("sh", "s")
    py = py.replace("ang", "an").replace("eng", "en").replace("ing", "in")
    return py


_WAKE_PY = _phon("贾维斯")   # "jiaweisi"


def _wake_match(text: str) -> tuple[bool, int]:
    """用拼音相似度判断是否含唤醒词。返回(是否命中, 命中片段结束的字符下标)。"""
    low = text.lower()
    if "jarvis" in low:
        return True, low.find("jarvis") + len("jarvis")
    chars = list(text)
    best_ratio, best_end = 0.0, 0
    for i in range(len(chars)):
        acc = ""
        for j in range(i, min(i + 4, len(chars))):
            acc += _phon(chars[j])
            if not acc:
                continue
            r = difflib.SequenceMatcher(None, acc, _WAKE_PY).ratio()
            if r > best_ratio:
                best_ratio, best_end = r, j + 1
    return best_ratio >= config.WAKE_SIM, best_end


def _has_wake_word(text: str) -> bool:
    return _wake_match(text)[0]


def _strip_wake_word(text: str) -> str:
    ok, end = _wake_match(text)
    rest = text[end:] if ok else text
    return rest.strip(_PUNCT)


def _cue() -> None:
    """唤醒提示音。"""
    if config.IS_WINDOWS:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_OK)
        except Exception:  # noqa: BLE001
            pass
        return
    subprocess.Popen(
        ["afplay", "/System/Library/Sounds/Glass.aiff"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---- UI 抽象：命令行 / 桌宠 共用同一套接口 --------------------------

class CliUI:
    """纯命令行：只把日志打到终端。"""

    def set_state(self, state: str) -> None: ...
    def log(self, text: str) -> None: print(text)
    def heard(self, text: str) -> None: ...
    def reply(self, text: str) -> None: print(f"🤖 贾维斯：{text}\n")
    def poll_talk(self) -> bool: return False


# ---- 语音助手主循环（在后台线程运行）-------------------------------

def run_assistant(ui, stop: threading.Event) -> None:
    api_key = config.load_api_key()          # main() 已校验过，这里必有
    ui.log("⏳ 正在加载语音识别模型（首次会下载，请稍候）…")
    asr.load()

    # 接入 MCP 工具（可选，配置在 mcp.json；起不来不影响主程序）
    from .mcp_bridge import McpBridge, load_config
    mcp = None
    mcp_cfg = load_config()
    if mcp_cfg:
        ui.log("⏳ 正在接入 MCP 工具（首次会下载服务器，请稍候）…")
        mcp = McpBridge()
        mcp.start(mcp_cfg, log=ui.log)

    brain = Brain(api_key, mcp=mcp)

    ui.log("⏳ 正在校准麦克风环境噪音，请保持安静…")
    with Microphone() as mic:
        mic.on_speech_start = lambda: ui.set_state("listening")
        ui.log(f"✓ 就绪！喊「贾维斯」或点一下桌宠唤醒我。（噪音阈值 {mic.threshold:.0f}）\n")

        awake_until = 0.0
        for audio in mic.segments():
            if stop.is_set():
                break

            clicked = ui.poll_talk()             # 点击桌宠 = 强制进入清醒
            if clicked:
                awake_until = time.time() + config.ACTIVE_TIMEOUT

            # 省配额 + 待机隐私：待机只用本地听唤醒词（免费、不上传，且唤醒靠拼音
            # 模糊匹配能容错）；唤醒后的命令才用讯飞云端（更准）。
            awake = time.time() < awake_until
            res = asr.transcribe(audio, cloud=awake)
            text = res.text

            if not text:
                if not awake:
                    ui.set_state("idle")
                continue
            ui.log(f"🎤 听到：{text}")

            if not awake:
                # 待机时严格过滤：噪音幻听 / 置信度低 / 太短 一律忽略
                if (res.no_speech > config.WAKE_MAX_NO_SPEECH
                        or res.avg_logprob < config.WAKE_MIN_LOGPROB
                        or len(text) < config.WAKE_MIN_LEN):
                    ui.set_state("idle")
                    continue
                if not _has_wake_word(text):
                    ui.set_state("idle")
                    continue
                _cue()
                command = _strip_wake_word(text)
                if not command:                    # 只喊了名字，应答后等命令
                    ui.set_state("speaking")
                    tts.speak("我在", blocking=True)
                    mic.flush()
                    ui.set_state("idle")
                    awake_until = time.time() + config.ACTIVE_TIMEOUT
                    continue
                # 「贾维斯+命令」连在一句：待机是本地转的，命令部分用讯飞重转一遍求更准
                if config.ASR_BACKEND == "xfyun":
                    cloud_res = asr.transcribe(audio, cloud=True)
                    if cloud_res.text:
                        command = _strip_wake_word(cloud_res.text)
            else:
                # 清醒时同样过滤噪音幻听，避免把背景声/自己的回声当成命令
                if (res.no_speech > config.WAKE_MAX_NO_SPEECH
                        or res.avg_logprob < config.WAKE_MIN_LOGPROB):
                    continue
                command = text

            ui.heard(command)
            ui.set_state("thinking")
            ui.log(f"💭 处理中：{command}")

            # 流式管线：后台线程边生成边把「整句」塞进队列，主线程取到就朗读。
            # 这样大模型生成与语音合成/播放重叠，大幅缩短「开口」等待。
            sq: "queue.Queue[tuple[str, str]]" = queue.Queue()

            def _produce(cmd: str = command) -> None:
                try:
                    for sent in brain.ask_stream(cmd):
                        sq.put(("s", sent))
                except Exception as e:  # noqa: BLE001
                    sq.put(("err", str(e)))
                finally:
                    sq.put(("end", ""))

            threading.Thread(target=_produce, daemon=True).start()

            parts: list[str] = []
            while True:
                kind, val = sq.get()
                if kind == "end":
                    break
                if kind == "err":
                    ui.log(f"  大脑出错：{val}")
                    continue
                if not parts:
                    ui.set_state("speaking")       # 第一句到了才切「说话」
                parts.append(val)
                ui.reply(val)
                tts.speak(val, blocking=True)      # 逐句朗读，句子间不打断

            reply = "".join(parts)
            if not reply:                          # 全程没产出文本（出错等）
                ui.set_state("speaking")
                tts.speak("抱歉，我这边出了点问题。", blocking=True)
            ui.log(f"🤖 贾维斯：{reply}\n")

            mic.flush()                            # 清掉朗读期间录进来的回声，防止自言自语
            ui.set_state("idle")
            awake_until = time.time() + config.ACTIVE_TIMEOUT


def main() -> int:
    use_pet = "--no-pet" not in sys.argv[1:]

    if not config.LLM_BASE_URL:
        print("✗ 没填中转站地址。", file=sys.stderr)
        print("  请在项目目录 base_url.txt 写入你的中转站地址（如 https://你的站/v1）。",
              file=sys.stderr)
        return 1
    if not config.load_api_key():
        print("✗ 没找到 API Key。", file=sys.stderr)
        print("  请在项目目录 api_key.txt 写入你中转站的 key（或设环境变量 JARVIS_API_KEY）。",
              file=sys.stderr)
        return 1
    print(f"🧠 大脑：{config.MODEL}  @ {config.LLM_BASE_URL}", file=sys.stderr)
    _asr = ("讯飞云端·语音听写" if config.ASR_BACKEND == "xfyun"
            else f"本地 whisper-{config.WHISPER_MODEL}")
    print(f"🎙️ 识别：{_asr}", file=sys.stderr)

    if use_pet:
        try:
            from .pet import DesktopPet
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 桌宠加载失败，改用命令行模式：{e}")
            use_pet = False

    stop = threading.Event()
    if use_pet:
        pet = DesktopPet()
        worker = threading.Thread(target=run_assistant, args=(pet, stop), daemon=True)
        worker.start()
        try:
            pet.run()                              # 主线程跑 GUI，关闭窗口即退出
        finally:
            stop.set()
    else:
        run_assistant(CliUI(), stop)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n👋 贾维斯已退出。")
