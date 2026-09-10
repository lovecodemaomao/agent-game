# -*- coding: utf-8 -*-
"""任务2.3/2.4：编排器兜底 + LLM 通道配额单测。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.framework.llmchannel import LLMChannel
from agents.framework.orchestrator import Orchestrator
from tests.util import load_demo_request

REQ = load_demo_request()


class SlowStrategy:
    def decide(self, request):
        time.sleep(10)
        return {}


class BoomStrategy:
    def decide(self, request):
        raise RuntimeError("boom")


class GoodStrategy:
    def decide(self, request):
        return {"roleCommandMap": {"10010": {"action": "move", "targetPos": [{"x": 5, "y": 22}]}}}


# -- 2.3 超时兜底 --
orch = Orchestrator(SlowStrategy(), LLMChannel(), round_budget=0.3)
resp = orch.handle(json.loads(json.dumps(REQ)))
assert resp["roleCommandMap"] == {}, f"超时应回退空: {resp}"
print("timeout fallback ok")

# -- 2.3 异常兜底 --
orch = Orchestrator(BoomStrategy(), LLMChannel(), round_budget=1.0)
resp = orch.handle(json.loads(json.dumps(REQ)))
assert resp["roleCommandMap"] == {}
print("exception fallback ok")

# -- 2.3 正常路径 + 上回合合法指令重放 --
orch = Orchestrator(GoodStrategy(), LLMChannel(), round_budget=1.0)
resp = orch.handle(json.loads(json.dumps(REQ)))
assert resp["roleCommandMap"]["10010"]["action"] == "move"
# 下回合策略失败 -> 重放上回合合法指令
resp2 = orch.handle(json.loads(json.dumps(REQ)))
assert resp2["roleCommandMap"]["10010"]["action"] == "move", "应重放上回合合法指令"
print("replay fallback ok")

# -- 2.4 LLM 通道配额 --
llm = LLMChannel(daily_quota=3)
r1 = json.loads(json.dumps(REQ))  # round 85, day 1
llm.observe(r1)
assert llm.can_send
assert llm.send("p1") and llm.used_today == 1
assert not llm.can_send, "outstanding 时不可再发"
llm.last_resp = ""; r1b = json.loads(json.dumps(REQ)); r1b["llmResp"] = "a1"; llm.observe(r1b)
assert llm.can_send
llm.send("p2"); r2 = dict(r1b, roundNo=86); r2["llmResp"] = "a2"; llm.observe(r2)
llm.send("p3"); r3 = dict(r2, roundNo=87); r3["llmResp"] = "a3"; llm.observe(r3)
assert not llm.can_send, "3次用尽"
# 次日重置
r4 = dict(r3, roundNo=131); r4["llmResp"] = ""; llm.observe(r4)
assert llm.can_send and llm.used_today == 0
# 任务期豁免（模拟逐回合：发prompt -> 次回合llmResp回填 -> 再发）
r5 = dict(r4, roundNo=132); r5["phaseTask"] = "查询北京天气"; llm.observe(r5)
for i in range(10):
    assert llm.can_send, f"任务期第{i}次应可发送"
    assert llm.send(f"task {i}")
    r6 = dict(r5, roundNo=133 + i); r6["llmResp"] = f"resp {i}"; llm.observe(r6)
assert llm.used_today == 0, "任务期调用不计入当日配额"
print("llm quota ok")

print("orchestrator+llmchannel tests: ALL PASS")
