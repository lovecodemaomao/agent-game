# -*- coding: utf-8 -*-
"""任务组11：OpponentModel / 教义旋钮 / 每日LLM分析兜底链 / 新闻推理与宝藏求解。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.competitor.agent import CompetitorAgent
from agents.competitor.doctrine import DoctrineParams, doctrine_index, load_doctrine
from agents.competitor.engine import CompetitorEngine
from agents.competitor.opponent_model import OpponentModel
from tests.util import load_demo_request

DEMO = load_demo_request()

# ---------------- 11.1 OpponentModel：对局记录离线回放验证 ----------------
from sim.engine.config import load_config
from sim.engine.provider import EngineProvider
from sim.judge.demo_provider import DemoProvider  # noqa

# 跑一场短对局（competitor占位 vs baseline），用记录回放校验 OpponentModel 捕获
import subprocess
subprocess.run([sys.executable, "-m", "sim.judge.runmatch", "--rounds", "260",
                "--record", "/tmp/om_match.jsonl", "--response-deadline", "5"],
               check=True, capture_output=True)

om = OpponentModel("challenger")
builds, kills, harass = 0, 0, 0
for line in open("/tmp/om_match.jsonl"):
    rec = json.loads(line)
    om.update(rec["teams"]["challenger"]["request"])
f = om.features()
for e in om.events:
    if e["kind"] == "enemy_build":
        builds += 1
    elif e["kind"] == "enemy_kills":
        kills += e["count"]
    elif e["kind"] == "enemy_harass":
        harass += 1
assert builds >= 1, f"应捕获对手建武器/建墙事件: {builds}"
assert isinstance(f["enemy_wall_count"], int), "围墙计数应为整数特征"
assert kills >= 0
print(f"11.1 opponent model ok: build_events={builds}, kill_events={kills}, "
      f"harass={harass}, wall_count={f['enemy_wall_count']}, features keys ok")

# ---------------- 11.2 教义×旋钮：夹紧 + 热替换 + 零LLM日常调度 ----------------
p = load_doctrine("turtle")
assert p.name == "turtle" and p.wall_density == 1.6
p.apply_overrides({"wall_density": 99, "task_tempo": -1, "harass_budget": 0.9,
                   "unknown_knob": 1})
assert p.wall_density == 2.0, "应夹紧到上界"
assert p.task_tempo == 0.0, "应夹紧到下界"
assert p.harass_budget == 0.5, "应夹紧到上界"
assert not hasattr(p, "unknown_knob"), "白名单外字段应忽略"
idx = doctrine_index()
assert {"name", "description"} <= set(idx[0]) and len(idx) >= 3
print("11.2 doctrine clamp ok, index:", [d["name"] for d in idx])

# 零LLM日常调度：普通回合 prompt 为空且指令经校验器出站
agent = CompetitorAgent()
req = json.loads(json.dumps(DEMO))
out = agent.decide(req)
assert out["prompt"] == "", "普通回合不应发 prompt"
assert set(out) == {"roleCommandMap", "prompt", "executeCmd"}
print("11.2 zero-LLM scheduling ok")

# ---------------- 11.3 每日LLM分析 + 兜底链 ----------------
# 11.3a 合法输出换参
req_day1 = json.loads(json.dumps(DEMO)); req_day1["roundNo"] = 1  # day1 第一回合
out1 = agent.decide(req_day1)
assert out1["prompt"] != "", "日界应发分析 prompt"
assert agent._analysis_sent_day == 1
assert agent.llm.used_today == 1
# 模拟次日 llmResp 回填合法 JSON：切到 turtle 并微调
req_day2 = json.loads(json.dumps(DEMO)); req_day2["roundNo"] = 131
req_day2["llmResp"] = json.dumps({"doctrine": "turtle",
                                  "overrides": {"wall_density": 99, "harass_budget": 0.4}})
agent.decide(req_day2)
assert agent.current.name == "turtle"
assert agent.current.wall_density == 2.0, "overrides 应被夹紧"
assert agent.current.harass_budget == 0.4
print("11.3a valid analysis applied + clamped ok")
# 11.3b 非法输出兜底（保持昨日教义）
bad = json.loads(json.dumps(req_day2)); bad["llmResp"] = "这不是JSON"
agent.current.name = "turtle"  # 确认当前状态
agent._consume_analysis(bad)
assert agent.current.name == "turtle", "非法输出应保持当前教义"
good_fallback = agent.current.copy()
print("11.3b invalid output fallback ok")

# ---------------- 11.4 新闻推理（囤卖套利）+ 宝藏求解 ----------------
eng = agent.engine
req_news = json.loads(json.dumps(DEMO))
req_news["roundNo"] = 20  # 白天（demo 原始 roundNo=85 是夜晚，会走夜间分支）
req_news["worldNews"]["officialNews"] = ("矿业管理局紧急通报：北部铁矿区发生塌方，明日全面停工2天，"
                                         "铁收购价将上涨至2倍")
wk = next(r for r in req_news["teamOur"]["roles"] if r["id"] == 10010)
wk["backpack"] = ["iron", "iron", "stone"]
wk["pos"] = {"x": 20, "y": 17}  # 紧邻小贩(20,16)
agent.engine.news_signal = {}
out_n = agent.engine.decide(req_news, agent.current)
sell_cmd = out_n["roleCommandMap"].get("10010")
assert sell_cmd and sell_cmd["action"] == "sell" and sell_cmd["name"] == "iron", sell_cmd
print("11.4a surge arbitrage ok (立即抛售持有铁)")

# 宝藏线索收敛（我们自己新闻格式的正则）
folk = ("【民间传闻】村东老叟临终留言：宝藏埋在挑战者营地东12里、北8里之处\n"
        "游方道士曰：开启宝藏需以「寒霜药剂」为引\n"
        "酒馆诗人吟唱：祭坛之前还需备齐星辰之沙、古符石板\n"
        "星象师推演：祭坛自第4天起方可开启\n"
        "古老石碑刻着：宝藏封印至第8天闭合，逾期永闭")
req_t = json.loads(json.dumps(DEMO)); req_t["roundNo"] = 1
req_t["worldNews"]["folkLegends"] = folk
eng2 = CompetitorEngine()
req_t["teamOur"]["goldNum"] = 500
req_pv = next(r for r in req_t["teamOur"]["roles"] if r["id"] == 10011)
for _ in range(6):  # 模拟6天线索累积（同文本幂等）
    req_t["worldNews"]["folkLegends"] = folk
    eng2._read_news(req_t)
h = eng2.treasure_hypothesis
assert h.get("pos") == {"x": 22, "y": 32}, h.get("pos")
assert set(h["items"]) == {"FrostPotion", "StarSand", "AcientTablet"}
assert h.get("window_start") == 4 and h.get("window_end") == 8
print("11.4b treasure hypothesis converge ok:", h["pos"], h["items"], h["window_start"])
# 高信心召唤执行：祭品齐 + 窗口内 -> summonTreasure
req_t2 = json.loads(json.dumps(DEMO)); req_t2["roundNo"] = 4 * 130 + 1
req_pv2 = next(r for r in req_t2["teamOur"]["roles"] if r["id"] == 10011)
req_pv2["backpack"] = ["FrostPotion", "StarSand", "AcientTablet"]
req_pv2["pos"] = {"x": 21, "y": 31}  # 宝藏(22,32)旁
eng2.treasure_hypothesis = h
eng2.treasure_done = False
out_s = eng2.decide(req_t2, agent.current)
sm = out_s["roleCommandMap"].get("10011")
assert sm and sm["action"] == "summonTreasure", sm
assert sorted(sm["item"]) == ["AcientTablet", "FrostPotion", "StarSand"]
print("11.4c high-confidence summon ok")

print("competitor core tests: ALL PASS")
