# -*- coding: utf-8 -*-
"""agent HTTP server 骨架。

以 `bash run.sh port` / `python -m agents.<x>.main --port N` 启动，
监听 0.0.0.0:port，POST / 接收判题器 request，返回编排器产出的 response。
GET /health 用于判题器就绪探测。
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict

log = logging.getLogger("agent.server")


def make_handler(on_request: Callable[[Dict[str, Any]], Dict[str, Any]]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默默认访问日志
            pass

        def do_GET(self):
            if self.path == "/health":
                body = json.dumps({"ok": True}).encode()
                self._send(200, body)
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                request = json.loads(raw.decode("utf-8"))
            except Exception as e:
                log.error("bad request: %s", e)
                self._send(400, json.dumps({"error": "bad request"}).encode())
                return
            try:
                response = on_request(request)
            except Exception as e:  # 永不崩溃：兜底空响应
                log.exception("handler error")
                response = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
            self._send(200, json.dumps(response, ensure_ascii=False).encode("utf-8"))

        def _send(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(port: int, on_request: Callable[[Dict[str, Any]], Dict[str, Any]]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(on_request))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("agent server listening on %s", port)
    return server
