# -*- coding: utf-8 -*-
"""任务组10：任务求解代理流水线 + SOP 复用 + 沙盒持久性。"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.framework.llmchannel import LLMChannel
from agents.framework.taskagent import TaskAgent, SOP_SCRIPT
from sim.engine.config import load_config
from sim.engine.resolve import Resolver
from sim.engine.state import WorldState
from sim.sandbox.exec import Sandbox
from sim.sandbox.mockapi import MockAPI, weather_of

TASK_DESC_TEMPLATE = ("【自进化任务】请查询{city}今天的天气并提交答案。\n"
                      "第三方天气API文档：GET {url}/weather?city=<城市名>\n"
                      "返回 JSON：{{\"city\": \"...\", \"weather\": \"...\"}}\n"
                      "完成后调用 submitAnswer，答案中需包含该城市的天气现象。")

api = MockAPI()
sb = Sandbox("taskagent_test", home=tempfile.mkdtemp(prefix="sandbox_ta_"),
             mock_api_url=api.url)
ta = TaskAgent(LLMChannel(daily_quota=3))  # 同一实例跨任务（一场比赛的生命周期）

# ---------------- 10.2 首次任务探索（LLM+沙盒双通道流水线）----------------
city1, city2 = "北京", "上海"
desc1 = TASK_DESC_TEMPLATE.format(city=city1, url=api.url)
r1 = {"phaseTask": desc1, "lastCmdResult": ""}
out1 = ta.step(r1, 10011)
assert out1["executeCmd"].startswith("curl"), "探索期应发 executeCmd"
assert "curl" in out1["prompt"], "探索期应同时发 prompt（双通道）"
res1 = sb.execute(out1["executeCmd"])
assert weather_of(city1) in res1, res1
r2 = {"phaseTask": desc1, "lastCmdResult": res1}
out2 = ta.step(r2, 10011)
cmd = out2["commands"]["10011"]
assert cmd["action"] == "submitAnswer" and weather_of(city1) in cmd["taskAnswer"], cmd
assert SOP_SCRIPT in out2["executeCmd"] and "base64" in out2["executeCmd"], "提交时应同步沉淀 SOP"
persist_result = sb.execute(out2["executeCmd"])
assert "exitCode:0" in persist_result, persist_result
print("10.2 first-task pipeline ok (explore+submit，提交回合同步沉淀 SOP)")

# ---------------- 10.3 SOP 复用（同类任务，零 LLM）----------------
desc2 = TASK_DESC_TEMPLATE.format(city=city2, url=api.url)
out3 = ta.step({"phaseTask": desc2, "lastCmdResult": ""}, 10011)
assert out3["executeCmd"].startswith("bash sop_weather.sh"), out3
assert out3["prompt"] == "", "SOP 通道不应消耗 LLM"
res2 = sb.execute(out3["executeCmd"])
assert weather_of(city2) in res2, res2
out4 = ta.step({"phaseTask": desc2, "lastCmdResult": res2}, 10011)
cmd2 = out4["commands"]["10011"]
assert cmd2["action"] == "submitAnswer" and weather_of(city2) in cmd2["taskAnswer"]
print("10.3 SOP reuse ok (同类任务 2 回合零 LLM 完成)")

# ---------------- 10.4 沙盒持久性（跨任务）----------------
sb.execute("echo persistent-marker > marker_10_4.txt")
probe = sb.execute("cat marker_10_4.txt")
assert "persistent-marker" in probe
script_check = sb.execute(f"test -f {SOP_SCRIPT} && echo SCRIPT_EXISTS")
assert "SCRIPT_EXISTS" in script_check
print("10.4 sandbox persistence ok -> design 开放问题3: 沙盒文件系统跨任务持久 = YES"
      "（agent 侧 SOP 记忆为进程内，跨进程需探针回合，已记录）")

# ---------------- 引擎侧全链路：接任务->探索->提交->结算 ----------------
cfg = load_config()
w = WorldState.create(cfg, seed=42)
res = Resolver(cfg, seed=42)
res.task_api_url = api.url
pv = w.units[10011]
tp = w.task_points["challenger"][0]
pv.pos = {"x": tp.positions[0]["x"] + 1, "y": tp.positions[0]["y"]}
res.step(w, {"challenger": {"10011": {"action": "acceptTask"}}, "defender": {}})
assert w.phase_task["challenger"], "任务应已领取"
ta2 = TaskAgent(LLMChannel(daily_quota=3))
o1 = ta2.step({"phaseTask": w.phase_task["challenger"], "lastCmdResult": ""}, 10011)
r_exec = sb.execute(o1["executeCmd"])
o2 = ta2.step({"phaseTask": w.phase_task["challenger"], "lastCmdResult": r_exec}, 10011)
res.step(w, {"challenger": o2["commands"], "defender": {}})
assert tp.active_task is None, "任务应已完成"
assert tp.cold_down == cfg.tasks["cooldown_rounds"], "应进入冷却"
assert w.teams["challenger"].score_task >= cfg.tasks["score_reward"], "应得任务分+速度加成"
print(f"engine-side task completion ok: score={w.teams['challenger'].score_task}")

print("task agent tests: ALL PASS")
