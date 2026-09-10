# -*- coding: utf-8 -*-
"""任务2.2 校验器单测：覆盖全部12个动作码的合法/非法样例。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.framework.validator import ValidationContext, validate_command, validate_response
from tests.util import load_demo_request

DEMO_REQUEST = load_demo_request()
CTX = ValidationContext(DEMO_REQUEST)


def ok(key, cmd):
    fixed, reason = validate_command(key, cmd, CTX)
    assert fixed is not None, f"应当合法但被拒: {reason}"
    return fixed


def bad(key, cmd, frag=""):
    fixed, reason = validate_command(key, cmd, CTX)
    assert fixed is None, f"应当非法但通过: {cmd}"
    if frag:
        assert frag in reason, f"原因[{reason}]应包含[{frag}]"
    return reason


# ---- move ----
ok("10010", {"action": "move", "targetPos": [{"x": 5, "y": 23}]})
bad("10010", {"action": "move"}, "targetPos")                      # 缺 targetPos
bad("10010", {"action": "move", "targetPos": [{"x": 99, "y": 0}]}, "越界")
bad("10010", {"action": "move", "targetPos": [{"x": "abc", "y": 0}]}, "非法")
bad("99999", {"action": "move", "targetPos": [{"x": 1, "y": 1}]}, "本队")

# ---- attack（夜晚，武器键10020 操控者10010）----
CTX.phase = "night"
ok("10020", {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 12, "y": 20}]})
bad("10020", {"action": "attack", "targetPos": [{"x": 1, "y": 1}]}, "controllerId")  # 缺
ok("10020", {"action": "attack", "controllerId": "10011", "targetPos": [{"x": 12, "y": 20}]})  # 开拓者也可操控（4.4: 全部）
bad("10020", {"action": "attack", "controllerId": "10013", "targetPos": [{"x": 1, "y": 1}]}, "角色")  # 建筑（基地）不能操控
bad("10010", {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 1, "y": 1}]}, "武器")  # 键非武器
bad("10020", {"action": "attack", "controllerId": "10010",
              "targetPos": [{"x": 1, "y": 1}, {"x": 2, "y": 2}]}, "数量")  # L1 两目标
# 锥形: 两目标方向夹角>90°
bad("10020", {"action": "attack", "controllerId": "10010",
              "targetPos": [{"x": 12, "y": 24}, {"x": 6, "y": 24}]}, "等级" if False else "")
# 上面武器在(9,24) 目标(12,24)dx=3,dy=0 与 (6,24)dx=-3,dy=0 点积<0 非法
# L2 加特林允许2目标：锥形内合法 / 锥形外非法
CTX.weapon_level[10020] = 2
CTX2_weapon_pos = CTX.weapon_pos[10020]  # (9,24)
ok("10020", {"action": "attack", "controllerId": "10010",
             "targetPos": [{"x": 12, "y": 24}, {"x": 11, "y": 21}]})   # dx=3,0 / dx=2,-3 点积>0
bad("10020", {"action": "attack", "controllerId": "10010",
              "targetPos": [{"x": 12, "y": 24}, {"x": 6, "y": 24}]}, "锥形")  # 对向
CTX.weapon_level[10020] = 1
CTX.phase = "day"
bad("10020", {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 1, "y": 1}]}, "夜晚")
CTX.phase = "night"

# ---- sell / buy / use / drop ----
ok("10010", {"action": "sell", "name": "stone", "num": 2})
bad("10010", {"action": "sell"}, "name")
bad("10010", {"action": "sell", "name": "gold"}, "sell.name")
ok("10011", {"action": "buy", "name": "Medicine"})
bad("10011", {"action": "buy", "num": 0, "name": "Medicine"}, "num")
ok("10010", {"action": "use", "name": "Medicine"})
bad("10010", {"action": "use", "name": "DizzyWeapon"}, "targetPos")     # 需坐标
ok("10010", {"action": "use", "name": "DizzyWeapon", "targetPos": [{"x": 5, "y": 5}]})
bad("10010", {"action": "use", "name": "NotAnItem"}, "未知物品")
ok("10010", {"action": "drop", "name": "stone"})
bad("10010", {"action": "drop"}, "name")

# ---- build / remove / collect（白天工人）----
CTX.phase = "day"
ok("10010", {"action": "build", "name": "wall", "targetPos": [{"x": 6, "y": 22}]})
bad("10010", {"action": "build", "name": "castle", "targetPos": [{"x": 6, "y": 22}]}, "build.name")
bad("10010", {"action": "build", "name": "wall"}, "targetPos")
bad("10011", {"action": "build", "name": "wall", "targetPos": [{"x": 6, "y": 22}]}, "工人")  # 开拓者
ok("10010", {"action": "remove", "targetPos": [{"x": 5, "y": 20}]})
ok("10010", {"action": "collect", "targetPos": [{"x": 4, "y": 24}]})
bad("10011", {"action": "collect", "targetPos": [{"x": 4, "y": 24}]}, "工人")
CTX.phase = "night"
bad("10010", {"action": "build", "name": "wall", "targetPos": [{"x": 6, "y": 22}]}, "白天")
CTX.phase = "day"

# ---- acceptTask / submitAnswer / summonTreasure（开拓者10011）----
ok("10011", {"action": "acceptTask"})
bad("10010", {"action": "acceptTask"}, "开拓者")
ok("10011", {"action": "submitAnswer", "taskAnswer": "晴 25 度"})
bad("10011", {"action": "submitAnswer"}, "taskAnswer")
ok("10011", {"action": "summonTreasure",
             "targetPos": [{"x": 16, "y": 16}], "item": ["AcientTablet", "StarSand"]})
bad("10011", {"action": "summonTreasure", "targetPos": [{"x": 16, "y": 16}]}, "item")
bad("10011", {"action": "summonTreasure", "targetPos": [{"x": 16, "y": 16}], "item": []}, "非空")
bad("10011", {"action": "summonTreasure", "item": ["x"]}, "targetPos")

# ---- 未知动作码 / 非对象 ----
bad("10010", {"action": "fly"}, "动作码非法")
bad("10010", "not-a-dict", "对象")

# ---- response 级清洗 ----
resp = {"roleCommandMap": {
    "10010": {"action": "move", "targetPos": [{"x": 5, "y": 22}]},
    "10011": {"action": "move"},                                # 非法：缺字段
    "10013": {"action": "fly"},                                 # 非法动作码
    "999": {"action": "move", "targetPos": [{"x": 1, "y": 1}]},  # 非本队
}, "prompt": "test", "executeCmd": None}
clean, dropped = validate_response(resp, CTX)
assert len(clean["roleCommandMap"]) == 1 and "10010" in clean["roleCommandMap"]
assert clean["executeCmd"] == ""
assert clean["prompt"] == "test"
assert len(dropped) == 3, dropped

# attack 时操控者不能另有指令
resp2 = {"roleCommandMap": {
    "10020": {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 12, "y": 20}]},
    "10010": {"action": "move", "targetPos": [{"x": 5, "y": 22}]},
}}
CTX.phase = "night"
clean2, dropped2 = validate_response(resp2, CTX)
assert "10010" not in clean2["roleCommandMap"] and "10020" in clean2["roleCommandMap"], dropped2

print("validator tests: ALL PASS")
