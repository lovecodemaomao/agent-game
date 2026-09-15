# -*- coding: utf-8 -*-
"""任务7.2/7.3：LLM 代理三上下文隔离 + 每日配额/任务期豁免（用假客户端，不打真API）。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.judge.proxy import LLMProxy


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, purpose="default"):
        self.calls.append((purpose, [m["content"] for m in messages]))
        # 若上下文里出现了别的上下文的内容即视为泄漏（由断言检查）
        return f"resp:{purpose}:{len(self.calls)}"


class FakeProvider:
    def __init__(self):
        self.llm_resp = {}


def make_rec(team, round_no, prompt="", phase_task=""):
    return {"roundNo": round_no, "teams": {team: {
        "request": {"phaseTask": phase_task, "roundNo": round_no},
        "response": {"prompt": prompt}}}}


llm_cfg = {"model": "fake", "base_url": "http://localhost:9", "timeout": 1, "retries": 0, "cache": False}

# -- 7.3 每日配额 --
provider = FakeProvider()
px = LLMProxy(llm_cfg, provider=provider, daily_quota=3)
px.client = FakeClient()  # 替换为假客户端
# 第1天(r1-130)：发3次通过，第4次拒绝
px.on_round(1, make_rec("challenger", 1, "p1"))
px.on_round(2, make_rec("challenger", 2, "p2"))  # p1仍在途 -> 不覆盖pending? (design: 每队每回合一个槽)
time.sleep(0.3)
px.on_round(3, make_rec("challenger", 3, "p3"))
time.sleep(0.3)
px.on_round(4, make_rec("challenger", 4, "p4"))
time.sleep(0.3)
px.on_round(5, make_rec("challenger", 5, "p5"))  # 第4次非任务期 -> 拒绝
assert px.quota["challenger"]["used"] == 3, px.quota
assert px.stats["challenger"]["rejected"] >= 1
assert len(px.errors["challenger"]) >= 1 and px.errors["challenger"][0]["errorCode"] == 5
# 第2天重置
px.on_round(131, make_rec("challenger", 131, "p6"))
assert px.quota["challenger"]["used"] == 1, px.quota
print("7.3 daily quota ok")

# -- 7.3 任务期豁免 --
provider2 = FakeProvider()
px2 = LLMProxy(llm_cfg, provider=provider2, daily_quota=3)
px2.client = FakeClient()
for i in range(6):
    px2.on_round(200 + i, make_rec("challenger", 200 + i, f"task {i}", phase_task="查询天气"))
    time.sleep(0.05)
assert px2.stats["challenger"]["quota_used"] == 0, "任务期不计配额"
assert px2.stats["challenger"]["total"] == 6
print("7.3 task exemption ok")

# -- 7.2 上下文隔离 --
provider3 = FakeProvider()
px3 = LLMProxy(llm_cfg, provider=provider3, daily_quota=99, keep_history=True)
fake = FakeClient()
px3.client = fake
px3.on_round(1, make_rec("challenger", 1, "我方机密计划X"))
time.sleep(0.2)
px3.on_round(2, make_rec("defender", 2, "我方机密计划Y"))
time.sleep(0.3)
# 检查：defender 的任何调用上下文中不得出现 challenger 的内容，反之亦然
for purpose, contents in fake.calls:
    if purpose == "agent:defender":
        assert not any("计划X" in c for c in contents), f"challenger 内容泄漏到 defender: {contents}"
    if purpose == "agent:challenger":
        assert not any("计划Y" in c for c in contents), f"defender 内容泄漏到 challenger: {contents}"
# keep_history 下 challenger 第二次调用应能看到自己的历史
px3.on_round(3, make_rec("challenger", 3, "回顾"))
time.sleep(0.2)
last_ch = [c for p, cs in fake.calls if p == "agent:challenger" for c in cs]
assert any("计划X" in c for c in last_ch), "challenger 应能看见自己的历史"
print("7.2 context isolation ok")

# -- 回填验证 --
time.sleep(0.3)
# 同步回填后无需轮询 pending：llm_resp 在 on_round 内已写入
assert "challenger" in provider3.llm_resp or px3.stats["challenger"]["total"] > 0
assert "challenger" in provider3.llm_resp
print("llm_resp backfill ok")

print("llm proxy tests: ALL PASS")
