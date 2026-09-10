# -*- coding: utf-8 -*-
"""沙盒内的 mock 第三方任务 API（任务9.2）：天气查询服务。

城市->天气映射确定性（引擎侧判答案与 API 返回一致）。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("sim.mockapi")

# 确定性天气表（引擎判答案与 API 返回共用此表）
WEATHER_TABLE = {
    "北京": "晴", "上海": "多云", "广州": "阵雨", "深圳": "雷阵雨",
    "成都": "阴", "杭州": "晴", "武汉": "高温", "西安": "沙尘",
    "南京": "小雨", "重庆": "雾",
}


def weather_of(city: str) -> str:
    return WEATHER_TABLE.get(city, "未知")


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/weather":
                city = (qs.get("city") or [""])[0]
                body = json.dumps({"city": city, "weather": weather_of(city)},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
            else:
                body = b'{"error":"not found"}'
                self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class MockAPI:
    def __init__(self, port: int = 0):
        self.server = ThreadingHTTPServer(("127.0.0.1", port), make_handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
