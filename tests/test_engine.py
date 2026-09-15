# -*- coding: utf-8 -*-
"""任务4.1-4.8 引擎 golden 单测。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import rules as R
from sim.engine.config import load_config
from sim.engine.resolve import Resolver
from sim.engine.state import Unit, WorldState

cfg = load_config()


def fresh(seed=42):
    w = WorldState.create(cfg, seed=seed)
    return w, Resolver(cfg, seed=seed)


def step1(w, res, ch=None, de=None):
    res.step(w, {"challenger": ch or {}, "defender": de or {}})


# ---------------- 4.1 初始化 ----------------
w, res = fresh()
assert w.width == 41 and w.height == 32
ch_units = [u for u in w.units.values() if u.team == "challenger"]
assert {u.role_type for u in ch_units} == {"station", "worker", "pioneer"}, "初始无武器（任务书4.5.1）"
assert w.teams["challenger"].gold == 75 and w.teams["defender"].gold == 75
assert w.units[10010].role_type == "worker" and w.units[10011].role_type == "pioneer"
assert w.units[20013].role_type == "station"
# 矿区不在可建造区域
for m in w.mines:
    assert w.zone_distance(m.pos, "challenger") > 6 and w.zone_distance(m.pos, "defender") > 6
print("4.1 init ok")


def add_weapon(w, team, kind, pos, level=1):
    """测试辅助：直接注入武器（跳过建造流程）。"""
    wid = R.TEAM_IDS[team][kind][0]
    w.units[wid] = Unit(wid, team, pos, kind, hp=R.ROLE_LEVEL_HP[kind][level], level=level)
    return w.units[wid]

# ---------------- 4.3 移动与碰撞 ----------------
w, res = fresh()
w.mines = []  # 移动测试不涉及矿区，排除随机占位干扰
wk = w.units[10010]
wk.pos = {"x": 5, "y": 22}
step1(w, res, ch={"10010": {"action": "move", "targetPos": [{"x": 5, "y": 23}]}})
assert wk.pos == {"x": 5, "y": 23} and w.last_results["challenger"][10010] is True
# 越一格以上非法
step1(w, res, ch={"10010": {"action": "move", "targetPos": [{"x": 8, "y": 23}]}})
assert wk.pos == {"x": 5, "y": 23} and w.last_results["challenger"][10010] is False
# 目标点被建筑占据（基地）
step1(w, res, ch={"10010": {"action": "move", "targetPos": [{"x": 9, "y": 24}]}})  # 加特林位置
assert wk.pos == {"x": 5, "y": 23} and w.last_results["challenger"][10010] is False
# 争夺：两个工人同回合移向同一格
w1, w2 = w.units[10010], w.units[10012]
w1.pos = {"x": 20, "y": 20}; w2.pos = {"x": 22, "y": 20}
step1(w, res, ch={"10010": {"action": "move", "targetPos": [{"x": 21, "y": 20}]},
                  "10012": {"action": "move", "targetPos": [{"x": 21, "y": 20}]}})
assert w1.pos == {"x": 20, "y": 20} and w2.pos == {"x": 22, "y": 20}
# 位置互换
step1(w, res, ch={"10010": {"action": "move", "targetPos": [{"x": 22, "y": 20}]},
                  "10012": {"action": "move", "targetPos": [{"x": 20, "y": 20}]}})
assert w1.pos == {"x": 20, "y": 20} and w2.pos == {"x": 22, "y": 20}
print("4.3 movement/collision ok")

# ---------------- 4.4 经济 ----------------
w, res = fresh()
wk = w.units[10010]
mine = w.mines[0]
wk.pos = {"x": mine.pos["x"] + 1, "y": mine.pos["y"]}
before = len(wk.backpack)
step1(w, res, ch={"10010": {"action": "collect", "targetPos": [mine.pos]}})
assert len(wk.backpack) == before + 1 and mine.remaining == 9
# 采10次枯竭后刷新（独立状态，避免中途刷新干扰）
from sim.engine.state import Mine
w, res = fresh()
mine = Mine(pos={"x": 15, "y": 15}, kind="stone", remaining=10)
w.mines = [mine]
wk = w.units[10010]
wk.pos = {"x": mine.pos["x"] + 1, "y": mine.pos["y"]}
old_pos = dict(mine.pos)
for i in range(10):
    step1(w, res, ch={"10010": {"action": "collect", "targetPos": [mine.pos]}})
assert mine.remaining == 0, mine.remaining
step1(w, res, ch={})  # 下回合刷新
assert len(w.mines) == 1, "单矿测试环境数量守恒"
assert mine.remaining == 10, "刷新后剩余量重置"
assert mine.pos != old_pos, "刷新位置应改变"
# 贩卖：工人携矿到小贩
wk2 = w.units[10012]
wk2.pos = {"x": w.zones[0]["pos"]["x"], "y": w.zones[0]["pos"]["y"] + 1}  # 任意位置先清背包
wk2.backpack = ["stone"]
wk2.pos = {"x": cfg.vendor[0], "y": cfg.vendor[1] + 1}
g0 = w.teams["challenger"].gold
step1(w, res, ch={"10012": {"action": "sell", "name": "stone", "num": 1}})
assert w.teams["challenger"].gold == g0 + 1 and wk2.backpack == []
# 购买：金币不足失败
wk2.backpack = []
g0 = w.teams["challenger"].gold
step1(w, res, ch={"10012": {"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 99}})
assert w.teams["challenger"].gold == g0, "金币不足购买应失败"
print("4.4 economy ok")

# ---------------- 4.5 建造/拆除/升级 ----------------
w, res = fresh()
wk = w.units[10010]
wk.pos = {"x": 7, "y": 20}
wk.backpack = ["stone"]
# 黄区建墙（距基地4-6：目标(6,21)距基地dx=4）
step1(w, res, ch={"10010": {"action": "build", "name": "wall", "targetPos": [{"x": 6, "y": 21}]}})
walls = [u for u in w.units.values() if u.role_type == "wall" and u.team == "challenger"]
assert any(x.pos == {"x": 6, "y": 21} for x in walls) and wk.backpack == []
# 白天不能attack已在校验器测过；蓝区建武器（官方demo口径: 距基地1）
wk2 = w.units[10012]
wk2.pos = {"x": 9, "y": 23}   # 距新footprint(左上角向右下) 目标(9,22)的相邻格
g0 = w.teams["challenger"].gold
step1(w, res, ch={"10012": {"action": "build", "name": "gatling", "targetPos": [{"x": 9, "y": 22}]}})
new_w = [u for u in w.units.values() if u.role_type == "gatling" and u.team == "challenger" and u.pos == {"x": 9, "y": 22}]
assert new_w and w.teams["challenger"].gold == g0 - 25
# 蓝区外建武器失败（距基地2在蓝区[1,1]之外）
wk2.pos = {"x": 9, "y": 21}
step1(w, res, ch={"10012": {"action": "build", "name": "rocket", "targetPos": [{"x": 8, "y": 21}]}})
assert not [u for u in w.units.values() if u.role_type == "rocket" and u.team == "challenger" and u.pos == {"x": 8, "y": 21}]
# 升级回满血
wall = walls[0]
wall.hp = 500
wk.backpack = ["WallUpgradeVoucher1"]
wk.pos = {"x": wall.pos["x"] + 1, "y": wall.pos["y"]}
step1(w, res, ch={"10010": {"action": "use", "name": "WallUpgradeVoucher1", "targetPos": [wall.pos]}})
assert wall.level == 2 and wall.hp == 1500 and wk.backpack == []
# 满级使用不消耗
wall.level = 3
wk.backpack = ["WallUpgradeVoucher2"]
step1(w, res, ch={"10010": {"action": "use", "name": "WallUpgradeVoucher2", "targetPos": [wall.pos]}})
assert wall.level == 3 and wk.backpack == ["WallUpgradeVoucher2"]
print("4.5 build/upgrade ok")

# ---------------- 4.6 武器攻击 ----------------
w, res = fresh()
gat = add_weapon(w, "challenger", "gatling", {"x": 9, "y": 24})          # id 10020
rail = add_weapon(w, "challenger", "railgun", {"x": 8, "y": 24}, level=2)  # id 10030, 能量20
r1 = Unit(30001, "robot", {"x": 12, "y": 24}, "smallRobot", hp=40, atk=5, target_team="challenger")
r2 = Unit(30002, "robot", {"x": 13, "y": 24}, "smallRobot", hp=40, atk=5, target_team="challenger")
for rr in (r1, r2):
    rr.abnormal = "dizzy"  # 全程定格，专注验证武器结算语义
w.units[r1.id] = r1; w.units[r2.id] = r2
wk = w.units[10010]; wk.pos = {"x": 8, "y": 24}  # 紧邻加特林(9,24)与狙击(8,24)
w.round_no = 71  # 夜晚
# 加特林L1：弹道(9,24)->(12,24) 最近命中 r1，10伤
step1(w, res, ch={"10020": {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 12, "y": 24}]}})
assert r1.hp == 30 and r2.hp == 40, (r1.hp, r2.hp)
# 电磁炮穿透（能量20）：路径 (8,24)->(13,24) 上先 r3(血5) 后 r1(血30)；机器人已定格
r3 = Unit(30003, "robot", {"x": 11, "y": 24}, "smallRobot", hp=5, atk=5, target_team="challenger")
r3.abnormal = "dizzy"
w.units[r3.id] = r3
step1(w, res, ch={"10030": {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 13, "y": 24}]}})
assert r3.hp <= 0, "近点机器人应被打穿"
assert r1.hp == 15, f"穿透扣能错误: {r1.hp}"
assert r2.hp == 40, f"能量耗尽后不应继续穿透: {r2.hp}"
# 火箭溅射：中心 r2(13,24) 20伤，相邻 r1 10伤
rock = add_weapon(w, "challenger", "rocket", {"x": 10, "y": 25})  # id 10040
wk2 = w.units[10012]; wk2.pos = {"x": 10, "y": 24}
step1(w, res, ch={"10040": {"action": "attack", "controllerId": "10012", "targetPos": [{"x": 13, "y": 24}]}})
assert r2.hp <= 20 and r1.hp <= 5, (r1.hp, r2.hp)
assert rock.cooldown == 3, "火箭发射后冷却"
# 冷却期再攻击无效
r4 = Unit(30004, "robot", {"x": 13, "y": 24}, "smallRobot", hp=40, atk=5, target_team="challenger")
w.units[r4.id] = r4
step1(w, res, ch={"10040": {"action": "attack", "controllerId": "10012", "targetPos": [{"x": 13, "y": 24}]}})
assert r4.hp == 40, "冷却期攻击应无效"
print("4.6 weapon attack ok")

# ---------------- 4.7 机器人 ----------------
w, res = fresh()
base = w.units[10013]
# 夜晚波次（step内部自增：设70 -> 进入71=夜1第1回合）
w.round_no = 70
step1(w, res)
robots = w.alive_robots()
assert len(robots) > 0, "夜晚应出现机器人"
assert all(r.target_team in ("challenger", "defender") for r in robots)
# 机器人攻击阻挡者：放一个机器人贴脸基地
r = Unit(30099, "robot", {"x": base.pos["x"] - 1, "y": base.pos["y"]}, "smallRobot",
         hp=40, atk=5, target_team="challenger")
w.units[r.id] = r
hp0 = base.hp
for _ in range(3):
    step1(w, res, ch={})
    if base.hp < hp0:
        break
assert base.hp < hp0, "机器人应攻击基地"
# 眩晕
r.abnormal = ""; r.alive = True
wk = w.units[10010]
wk.pos = {"x": r.pos["x"] + 1, "y": r.pos["y"]}
wk.backpack = ["DizzyWeapon"]
r.pos = {"x": wk.pos["x"] + 1, "y": wk.pos["y"]}
step1(w, res, ch={"10010": {"action": "use", "name": "DizzyWeapon", "targetPos": [dict(r.pos)]}})
assert r.abnormal == "dizzy"
# 清晨清除（设130 -> 进入131=第2天白天第1回合）
step1(w, res)
w.round_no = 130
step1(w, res)
assert len(w.alive_robots()) == 0
print("4.7 robots ok")

# ---------------- 4.8 积分与胜负 ----------------
w, res = fresh()
ts = w.teams["challenger"]
# 击杀积分
r = Unit(30098, "robot", {"x": 5, "y": 5}, "middleRobot", hp=1, atk=0, target_team="challenger")
w.units[r.id] = r
w.round_no = 71
w.pending_damage[r.id] = 10
res._settle_damage(w)
assert w.teams["challenger"].score_kill == 2
# 生存积分（每日最后回合）
w.round_no = 70  # day1 最后回合
res._end_of_round(w, 1)
assert ts.score_survive == 10
print("4.8 scoring ok")

print("engine golden tests: ALL PASS")
