#!/usr/bin/env python3.14
"""MiniMax API 本地代理服务 - 流式转发"""

import http.server
import json
import os
import urllib.request
import time
import argparse

API_HOST = "https://api.minimaxi.com"
MODEL = "MiniMax-M2.7"

RETRY_MAX = 3
RETRY_BASE_SLEEP = 5


def require_api_key():
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        print("错误: MINIMAX_API_KEY 环境变量未设置")
        exit(1)
    return api_key


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

    with urllib.request.urlopen(req) as resp:
        return resp


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_request_info(self):
        print(f"[proxy] {self.command} {self.path} → ", end="", flush=True)

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

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
        messages.append({"role": "user", "content": question})

        for attempt in range(1, RETRY_MAX + 1):
            try:
                resp = chat_completion(messages, require_api_key())
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
        self.end_headers()

        for chunk in resp:
            if not chunk:
                continue
            line = chunk.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # MiniMax stream chunk: {"id":"...","choices":[...]}
            try:
                data = json.loads(line)
                choices = data.get("choices", [{}])
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content is not None:
                    sse = json.dumps({"content": content, "done": False})
                    self.wfile.write(f"data: {sse}\n\n".encode("utf-8"))
            except json.JSONDecodeError:
                pass

        done = json.dumps({"content": "", "done": True})
        self.wfile.write(f"data: {done}\n\n".encode("utf-8"))


def run(port):
    server = http.server.HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"[proxy] listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniMax API 本地代理服务")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    args = parser.parse_args()
    run(args.port)