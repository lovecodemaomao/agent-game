# -*- coding: utf-8 -*-
"""任务8.1-8.3：新闻系统单测。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.engine.config import load_config
from sim.engine.state import WorldState
from sim.news.system import NewsSystem, ITEM_CN

cfg = load_config()

# ---------------- 8.1 确定性采样 ----------------
n1 = NewsSystem(cfg, seed=42)
n2 = NewsSystem(cfg, seed=42)
n3 = NewsSystem(cfg, seed=43)
assert n1.treasure["pos"] == n2.treasure["pos"], "相同种子应产出相同宝藏地点"
assert n1.treasure["items_needed"] == n2.treasure["items_needed"]
assert n1.treasure["pos"] != n3.treasure["pos"] or n1.treasure["items_needed"] != n3.treasure["items_needed"]
t = n1.treasure
assert len(t["items_needed"]) == cfg.treasure["items_needed"]
assert t["window"] == (cfg.treasure["window_start"], cfg.treasure["window_end"])
print("8.1 deterministic sampling ok:", t["pos"], [ITEM_CN[i] for i in t["items_needed"]], t["window"])

# ---------------- 8.2 线索链收敛 ----------------
w = WorldState.create(cfg, seed=42)
ns = NewsSystem(cfg, seed=42)
legends = []
for day in range(1, 6):
    ns.new_day(day, w)
    legends.append(w.world_news["folkLegends"])
full = "\n".join(legends)
# 从线索可唯一推断：地点（相对挑战者基地的东西/南北里数）、祭品、窗口
pos = t["pos"]
dx, dy = pos["x"] - 10, pos["y"] - 24
d1 = legends[0]
assert (f"{'东' if dx >= 0 else '西'}{abs(dx)}里" in d1) and (f"{'北' if dy >= 0 else '南'}{abs(dy)}里" in d1), d1
for it in t["items_needed"]:
    assert ITEM_CN[it] in full, f"祭品 {it} 线索缺失"
assert f"第{t['window'][0]}天" in full and f"第{t['window'][1]}天" in full
print("8.2 clue chain converges ok")
for i, l in enumerate(legends, 1):
    print(f"  DAY{i}: {l[:60]}...")

# ---------------- 8.3 塌方三段式影响 ----------------
w = WorldState.create(cfg, seed=42)
ns = NewsSystem(cfg, seed=42)
# 强制注入塌方事件：铁矿，停采2天，价格x2
ns._event = {"kind": "collapse", "ore": "iron", "start_day": 2, "duration": 2, "price_mult": 2.0}
# day1：事件未生效
ns._apply_event_effects(1, w)
assert w.news_prices["iron"] == cfg.economy["base_prices"]["iron"]
# day2-3：停采+涨价
for d in (2, 3):
    ns._apply_event_effects(d, w)
    assert w.news_prices["iron"] == cfg.economy["base_prices"]["iron"] * 2, d
    assert w.news_mine_blocked["iron"] > 0, d
# day4：恢复
ns._apply_event_effects(4, w)
assert w.news_prices["iron"] == cfg.economy["base_prices"]["iron"]
assert w.news_mine_blocked["iron"] == 0
# 停采使 collect 失败
from sim.engine.resolve import Resolver
w2, res = WorldState.create(cfg, seed=42), Resolver(cfg, seed=42)
iron_mine = next(m for m in w2.mines if m.kind == "iron")
wk = w2.units[10010]
wk.pos = {"x": iron_mine.pos["x"] + 1, "y": iron_mine.pos["y"]}
w2.news_mine_blocked["iron"] = 2
res.step(w2, {"challenger": {"10010": {"action": "collect", "targetPos": [iron_mine.pos]}}, "defender": {}})
assert w2.last_results["challenger"][10010] is False, "停采期间采集应失败"
print("8.3 collapse effect ok")

# ---------------- 8.3b 祭品错误消耗（引擎侧） ----------------
w3 = WorldState.create(cfg, seed=42)
res3 = Resolver(cfg, seed=42)
w3.treasure = {"pos": {"x": 20, "y": 20}, "items_needed": ["AcientTablet", "StarSand", "FlameBreath"],
               "window": (4, 8)}
pv = w3.units[10011]
pv.pos = {"x": 20, "y": 21}
pv.backpack = ["AcientTablet", "StarSand", "FrostPotion"]  # 第三件错误
res3.step(w3, {"challenger": {"10011": {"action": "summonTreasure",
                                        "targetPos": [{"x": 20, "y": 20}],
                                        "item": ["AcientTablet", "StarSand", "FrostPotion"]}},
               "defender": {}})
assert w3.last_summon_result["challenger"] == 3, w3.last_summon_result
assert pv.backpack == [], "错误祭品也应被消耗"
print("8.3b wrong offering consumed (code 3) ok")

print("news system tests: ALL PASS")
