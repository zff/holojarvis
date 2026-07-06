"""HoloUI —— 把语音助手的状态桥接到浏览器里的全息粒子页面。

实现与 DesktopPet 相同的 UI 协议(set_state / log / heard / reply / poll_talk / run),
内部起一个本地 HTTP 服务:
  GET  /            粒子页面(index.html 及 vendor 静态资源)
  GET  /events      SSE 事件流,推送 {type: state|heard|reply|log, ...}
  POST /talk        页面点击粒子核心 → 等价于点桌宠,强制唤醒

用法: python -m jarvis --holo
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8642
ROOT = Path(__file__).parent
REPO = ROOT.parent.parent


def _read_telemetry(state: dict) -> dict:
    """采集一轮系统遥测(mac 优先,失败的项直接省略)。"""
    out: dict = {}
    try:
        ncpu = os.cpu_count() or 8
        out["cpu"] = min(1.0, os.getloadavg()[0] / ncpu)
    except (OSError, AttributeError):
        pass
    try:
        du = shutil.disk_usage("/")
        out["disk"] = (du.total / 1e9, du.free / 1e9)
    except OSError:
        pass
    try:
        raw = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=3)
        line = raw.strip().splitlines()[-1]
        pct = next((int(t[:-1]) for t in line.replace(";", " ").split()
                    if t.endswith("%")), None)
        out["batt"] = (pct, "charging" in line or "charged" in line)
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = subprocess.check_output(["sysctl", "-n", "kern.boottime"],
                                      text=True, timeout=3)
        boot = float(raw.split("sec =")[1].split(",")[0].strip())
        secs = max(0, int(time.time() - boot))
        d, rem = divmod(secs, 86400)
        h, m = rem // 3600, rem % 3600 // 60
        out["uptime"] = f"{d}D {h:02d}H {m:02d}M" if d else f"{h:02d}H {m:02d}M"
    except Exception:  # noqa: BLE001
        pass
    if state.get("weather"):
        out["weather"] = state["weather"]
    return out


def _read_notes() -> list[str]:
    nf = REPO / "notes.txt"
    if nf.exists():
        items = [ln.strip().lstrip("-•* ").strip()
                 for ln in nf.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if items:
            return items
    mj = REPO / "memory.json"
    if mj.exists():
        try:
            data = json.loads(mj.read_text(encoding="utf-8"))
            return [it.get("fact", "") for it in data if it.get("fact")]
        except (json.JSONDecodeError, OSError):
            pass
    return ["（暂无笔记 · 编辑 notes.txt 或对我说「记住…」）"]


class _Hub:
    """SSE 广播中心:每个浏览器连接一个队列,新连接先补发当前状态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue[str]] = []
        self._last_state = "idle"

    def attach(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=200)
        q.put(json.dumps({"type": "state", "value": self._last_state}))
        with self._lock:
            self._clients.append(q)
        return q

    def detach(self, q: queue.Queue[str]) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def push(self, event: dict) -> None:
        if event.get("type") == "state":
            self._last_state = event["value"]
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:  # 掉线的客户端,丢弃即可
                pass


class HoloUI:
    """与 DesktopPet 同接口,把事件转发给浏览器页面。"""

    def __init__(self, port: int = PORT, open_browser: bool = True) -> None:
        self._hub = _Hub()
        self._talk = threading.Event()
        self._port = port
        self._open_browser = open_browser
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._weather_state: dict = {}
        threading.Thread(target=self._weather_loop, daemon=True).start()
        threading.Thread(target=self._telemetry_loop, daemon=True).start()

    # ---- 后台数据推送 ------------------------------------------------
    def _weather_loop(self) -> None:
        while True:
            try:
                req = urllib.request.Request("https://wttr.in/?format=%t|%C",
                                             headers={"User-Agent": "curl/8"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    raw = r.read().decode("utf-8").strip()
                if "|" in raw and "Unknown" not in raw:
                    t, c = raw.split("|", 1)
                    self._weather_state["weather"] = (
                        t.replace("+", "").strip(), c.strip().upper())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1200)

    def _telemetry_loop(self) -> None:
        while True:
            self._hub.push({"type": "telemetry",
                            "value": _read_telemetry(self._weather_state)})
            self._hub.push({"type": "notes", "value": _read_notes()})
            time.sleep(3)

    # ---- UI 协议 ----------------------------------------------------
    def set_state(self, state: str) -> None:
        self._hub.push({"type": "state", "value": state})

    def log(self, text: str) -> None:
        print(text)
        self._hub.push({"type": "log", "value": text})

    def heard(self, text: str) -> None:
        self._hub.push({"type": "heard", "value": text})

    def reply(self, text: str) -> None:
        self._hub.push({"type": "reply", "value": text})

    def poll_talk(self) -> bool:
        clicked = self._talk.is_set()
        self._talk.clear()
        return clicked

    # ---- HTTP 服务 --------------------------------------------------
    def run(self) -> None:
        ui = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            extensions_map = {
                **http.server.SimpleHTTPRequestHandler.extensions_map,
                ".mjs": "text/javascript",
                ".js": "text/javascript",
                ".wasm": "application/wasm",
                ".task": "application/octet-stream",
            }

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(ROOT), **kwargs)

            def log_message(self, *args):  # 静默访问日志
                pass

            def do_POST(self):
                if self.path == "/talk":
                    ui._talk.set()
                    self.send_response(204)
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_GET(self):
                if self.path != "/events":
                    return super().do_GET()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = ui._hub.attach()
                try:
                    while True:
                        try:
                            data = q.get(timeout=15)
                            self.wfile.write(f"data: {data}\n\n".encode())
                        except queue.Empty:  # 心跳,顺便探测断连
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    ui._hub.detach(q)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        url = f"http://127.0.0.1:{self._port}/"
        print(f"🌀 全息形象已启动: {url}")
        if self._open_browser:
            threading.Timer(0.4, webbrowser.open, args=(url,)).start()
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
