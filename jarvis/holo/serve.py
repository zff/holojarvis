"""一键启动全息粒子页面。

摄像头 getUserMedia 只在安全上下文(https/localhost)可用,
file:// 直接打开会拿不到摄像头,所以起一个本地静态服务再开浏览器。

用法: python3 jarvis/holo/serve.py [端口]
"""
import http.server
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
ROOT = Path(__file__).parent


class Handler(http.server.SimpleHTTPRequestHandler):
    # 浏览器要求 ES 模块 / WASM 用正确的 MIME 类型,否则拒绝加载
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".mjs": "text/javascript",
        ".js": "text/javascript",
        ".wasm": "application/wasm",
        ".task": "application/octet-stream",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):  # 静默日志
        pass


def main() -> None:
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}/"
        print(f"⚡ HoloJarvis 粒子核心已启动: {url}  (Ctrl+C 退出)")
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已关闭。")


if __name__ == "__main__":
    main()
