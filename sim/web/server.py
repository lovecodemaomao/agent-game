# -*- coding: utf-8 -*-
"""前端服务：静态页 + 对局记录 API（标准库实现，无构建链）。

  GET /                     -> index.html
  GET /api/matches          -> 对局列表
  GET /api/match?name=xxx   -> 单场 JSONL 解析为数组
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sim.engine.config import repo_path

log = logging.getLogger("sim.web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MATCHES_DIR = repo_path("matches")


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                return self._static("index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], None)
            if path == "/api/matches":
                files = sorted(f for f in os.listdir(MATCHES_DIR)
                               if f.endswith(".jsonl")) if os.path.isdir(MATCHES_DIR) else []
                return self._json({"matches": files})
            if path == "/api/match":
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                name = (qs.get("name") or [""])[0]
                safe = os.path.basename(name)  # 防目录穿越
                fp = os.path.join(MATCHES_DIR, safe)
                if not os.path.isfile(fp):
                    return self._json({"error": "not found"}, 404)
                rounds = []
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rounds.append(json.loads(line))
                return self._json({"name": safe, "rounds": rounds})
            self._json({"error": "not found"}, 404)

        def _static(self, name, ctype_default):
            safe = os.path.basename(name)
            fp = os.path.join(STATIC_DIR, safe)
            if not os.path.isfile(fp):
                return self._send(404, b"not found", "text/plain")
            ctype = ctype_default or {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(os.path.splitext(safe)[1], "application/octet-stream")
            with open(fp, "rb") as f:
                self._send(200, f.read(), ctype)

    return Handler


def serve(port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("web ui on http://127.0.0.1:%s", port)
    return server


def main() -> None:
    import time
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("WEB_PORT", "8080"))
    serve(port)
    print(f"前端已启动: http://127.0.0.1:{port}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
