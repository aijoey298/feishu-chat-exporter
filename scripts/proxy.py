#!/usr/bin/env python3.14
"""MiniMax API 本地代理服务 - 流式转发"""

import http.server
import json
import os
import re
import urllib.request
import time
import argparse
from pathlib import Path

API_HOST = "https://api.minimaxi.com"
MODEL = "MiniMax-M2.7"

RETRY_MAX = 3
RETRY_BASE_SLEEP = 5


_api_key = None

def _load_key_from_config():
    """从 crayon-shinchan config.js 读取 API Key 作为 fallback"""
    import re
    from pathlib import Path
    for cfg_path in [
        Path.home() / "openclaw/lume/workspace/漫画生成/crayon-shinchan/config.js",
    ]:
        try:
            if cfg_path.exists():
                text = cfg_path.read_text()
                m = re.search(r"apiKey:\s*'([^']+)'", text)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return ""

def get_api_key():
    global _api_key
    if _api_key is None:
        _api_key = os.environ.get("MINIMAX_API_KEY", "")
        if not _api_key:
            _api_key = _load_key_from_config()
        if not _api_key:
            print("错误: MINIMAX_API_KEY 环境变量未设置")
            return None
    return _api_key


def chat_completion(messages, api_key):
    url = f"{API_HOST}/v1/text/chatcompletion_v2"
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    return urllib.request.urlopen(req)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_request_info(self):
        print(f"[proxy] {self.command} {self.path} → ", end="", flush=True)

    def send_json(self, data, status=200, cors=False):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if data:
            self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        self.log_request_info()
        if self.path == "/ask":
            print("204")
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        else:
            print("404")
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        self.log_request_info()
        if self.path == "/health":
            print("200")
            self.send_json({"status": "ok"})
        else:
            print("404")
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.log_request_info()
        if self.path != "/ask":
            print("404")
            self.send_json({"error": "not found"}, 404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            question = data.get("question", "")
            history = data.get("history", [])
            context = data.get("context", None)  # 完整上下文字符串（包含 {question} 占位符）
        except (json.JSONDecodeError, KeyError) as e:
            print(f"400: {e}")
            self.send_json({"error": "invalid request"}, 400)
            return

        messages = []
        for h in history:
            if h.get("role") == "user":
                messages.append({"role": "user", "content": h.get("content", "")})
            elif h.get("role") == "assistant":
                messages.append({"role": "assistant", "content": h.get("content", "")})

        if context:
            # 新协议：context 包含完整上下文（system + 全部消息 + {question} 占位符）
            actual_content = context.replace("{question}", question)
            messages.append({"role": "user", "content": actual_content})
        else:
            # 旧协议（向后兼容）：只用 question
            messages.append({"role": "user", "content": question})

        api_key = get_api_key()
        if not api_key:
            self.send_json({"error": "MINIMAX_API_KEY not set"}, 500)
            return

        for attempt in range(1, RETRY_MAX + 1):
            try:
                resp = chat_completion(messages, api_key)
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) and attempt < RETRY_MAX:
                    sleep_time = RETRY_BASE_SLEEP * attempt
                    print(f"{e.code}, retry #{attempt} in {sleep_time}s")
                    time.sleep(sleep_time)
                else:
                    print(f"{e.code}")
                    self.send_json({"error": f"upstream error: {e.code}"}, 502)
                    return
            except Exception as e:
                print(f"500: {e}")
                self.send_json({"error": str(e)}, 500)
                return

        print("200")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        for raw in resp:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data: "):
                continue

            # MiniMax SSE chunk: "data: {...}"
            try:
                data = json.loads(line[6:])  # strip "data: " prefix
                choices = data.get("choices", [{}])
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    sse = json.dumps({"content": content, "done": False})
                    self.wfile.write(f"data: {sse}\n\n".encode("utf-8"))
            except json.JSONDecodeError:
                pass

        done = json.dumps({"content": "", "done": True})
        self.wfile.write(f"data: {done}\n\n".encode("utf-8"))


def run(port):
    if not get_api_key():
        print("错误: MINIMAX_API_KEY 环境变量未设置，请在启动前设置该环境变量")
        exit(1)
    server = http.server.HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"[proxy] listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniMax API 本地代理服务")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    args = parser.parse_args()
    run(args.port)