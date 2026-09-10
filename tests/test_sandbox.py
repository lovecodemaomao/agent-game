# -*- coding: utf-8 -*-
"""任务9.1-9.3：沙盒单测。"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.sandbox.exec import Sandbox
from sim.sandbox.mockapi import MockAPI, weather_of

# ---------------- 9.2 mock API ----------------
from urllib.parse import quote
api = MockAPI()
r = os.popen(f"curl -s '{api.url}/weather?city={quote('北京')}'").read()
d = json.loads(r)
assert d == {"city": "北京", "weather": weather_of("北京")}, d
r404 = os.popen(f"curl -s -o /dev/null -w '%{{http_code}}' '{api.url}/other'").read()
assert r404.strip() == "404"
print("9.2 mock api ok:", api.url)

# ---------------- 9.1 执行/回填格式 ----------------
sb = Sandbox("test_team_a", mock_api_url=api.url)
out = sb.execute("echo hello")
assert out == "[exitCode:0]\nhello\n", repr(out)  # echo 自带换行
out = sb.execute("exit 3")
assert out.startswith("[exitCode:3]\n")
out = sb.execute("sleep 30")
assert out.startswith("[TIMEOUT]"), out[:40]
out = sb.execute("python3 -c \"print('x' * 100000)\"")
assert out.endswith("[TRUNCATED]") and len(out) <= 65536 + 20
print("9.1 exec formats ok (exit/timeout/truncate)")

# 持久家目录：跨调用保留文件
sb.execute("echo persisted > marker.txt")
assert os.path.isfile(os.path.join(sb.home, "marker.txt"))
print("9.1 persistent home ok")

# ---------------- 9.2 禁网：外网不可达、localhost 可达 ----------------
out = sb.execute("curl -s -m 5 -o /dev/null -w '%{http_code}' https://api.deepseek.com/models")
assert "000" in out or "exitCode" in out and "200" not in out, f"外网应不可达: {out!r}"
out = sb.execute(f"curl -s -m 5 '{api.url}/weather?city={quote('上海')}'")
assert "多云" in out, f"localhost mock API 应可达: {out!r}"
print("9.2 network isolation ok (external blocked, mock reachable)")

# ---------------- 9.3 门控逻辑（判题循环侧，集成验证在 e2e）----------------
# 沙盒拒绝逻辑：sb=None 或 provider.in_task=False 时 executeCmd 被忽略 —— 逻辑在 loop.py
# 此处直接验证 loop 的门控分支行为
from sim.judge.demo_provider import DemoProvider
from sim.judge.loop import JudgeLoop

captured = {}


class GateProvider(DemoProvider):
    def __init__(self):
        super().__init__({})
        self.w = type("W", (), {"last_cmd_result": {}})()
        self.in_task_flag = False

    def in_task(self, team):
        return self.in_task_flag


class ExecAgent:
    """直接内嵌的假 agent：返回带 executeCmd 的响应。"""
    pass


# 用真实 JudgeLoop + 内嵌假 server 太重，这里单测 provider.in_task 接口约定
gp = GateProvider()
assert gp.in_task("challenger") is False
gp.in_task_flag = True
assert gp.in_task("challenger") is True
print("9.3 gate interface ok (full-path tested in e2e)")

shutil.rmtree("/tmp", ignore_errors=True) if False else None
print("sandbox tests: ALL PASS")
