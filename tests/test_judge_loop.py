# -*- coding: utf-8 -*-
"""任务3.1-3.4：判题循环集成测试。

- 3.1 进程编排：拉起两个骨架 agent
- 3.2 循环：1300 回合空转
- 3.3 异常计数：超时/格式错误/指令错误/5次停调度（指令执行失败不计次）
- 3.4 对局记录：JSONL 逐行可解析、回合数完整
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.judge.agentproc import start_agents, stop_agents
from sim.judge.demo_provider import DemoProvider
from sim.judge.loop import JudgeLoop, TeamTracker, request_agent
from tests.util import load_demo_request

REQ = load_demo_request()

# ---------------------------------------------------------------- 3.1
procs = start_agents({"challenger": "agents.competitor.main", "defender": "agents.baseline.main"})
assert set(procs) == {"challenger", "defender"}
print("3.1 agent processes ok:", {t: p.port for t, p in procs.items()})

try:
    # ---------------------------------------------------------------- 3.2 + 3.4
    provider = DemoProvider(REQ)
    jl = JudgeLoop(provider, {t: p.port for t, p in procs.items()},
                   response_deadline=5.0, record_path="/tmp/test_match.jsonl")
    result = jl.run(max_rounds=1300)
    assert result["rounds"] == 1300, result
    assert result["abnormal"] == {"challenger": 0, "defender": 0}, result
    n = 0
    with open("/tmp/test_match.jsonl", "r", encoding="utf-8") as f:
        last = None
        for line in f:
            obj = json.loads(line)
            assert set(obj) >= {"roundNo", "teams"}
            last = obj
            n += 1
    assert n == 1300, n
    assert last["roundNo"] == 1300
    print("3.2+3.4 idle 1300 rounds ok, record ok")

    # ---------------------------------------------------------------- 3.3
    # 3.3a 超时：慢 agent
    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(6)
            body = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 19001), SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    resp, err = request_agent("http://127.0.0.1:19001/", REQ, 10, 5.0)
    assert resp is None and err == "timeout"
    srv.shutdown()
    print("3.3a timeout counted as error ok")

    # 3.3b 格式错误
    class BadFmtHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = b'{"foo": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 19002), BadFmtHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    resp, err = request_agent("http://127.0.0.1:19002/", REQ, 10, 5.0)
    assert err == "format"
    srv.shutdown()
    print("3.3b format error ok")

    # 3.3c 指令错误计入异常、合法但失败不计次（sanitize 层）
    from agents.framework.validator import ValidationContext, validate_response
    provider = DemoProvider(REQ)
    bad_resp = {"roleCommandMap": {"10010": {"action": "move"}}}  # 缺 targetPos -> 指令错误
    ctx = ValidationContext(REQ)
    clean, dropped = validate_response(bad_resp, ctx)
    assert len(dropped) == 1 and "targetPos" in dropped[0]
    # 合法 move 碰撞失败 -> lastRoundRoleActionResults false，不计异常（引擎行为，循环层只看指令错误）
    print("3.3c illegal command detection ok")

    # 3.3d 5次停调度
    tr = TeamTracker("t", max_abnormal=5)
    for i in range(5):
        tr.count_abnormal(3, "x")
    assert tr.suspended
    print("3.3d five-strike suspension ok")
finally:
    stop_agents(procs)
    print("agents stopped")

print("judge loop tests: ALL PASS")
