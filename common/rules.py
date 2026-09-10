# -*- coding: utf-8 -*-
"""跨 sim 与 agents 共享的比赛常量与纯函数。

时间公式、物品清单、动作码全集等规则口径的唯一权威，
引擎与双方 agent 必须从这里引用，禁止各自硬编码。
"""
from __future__ import annotations

# ---- 动作码全集（《接口文档》2.3）----
ACTIONS = {
    "move", "attack", "sell", "buy", "build", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop", "collect",
}

# ---- 各动作必填/可选字段（接口文档 2.2）----
# required: 缺失即"指令错误"（计入异常）
# optional: 可缺省
COMMAND_FIELDS = {
    "move":           {"required": ["targetPos"]},
    "attack":         {"required": ["controllerId", "targetPos"]},
    "sell":           {"required": ["name"], "optional": ["num"]},
    "buy":            {"required": ["name"], "optional": ["num"]},
    "build":          {"required": ["name", "targetPos"]},
    "remove":         {"required": ["targetPos"]},
    "acceptTask":     {"required": []},
    "submitAnswer":   {"required": ["taskAnswer"]},
    "summonTreasure": {"required": ["targetPos", "item"]},
    "use":            {"required": ["name"], "optional": ["targetPos"]},
    "drop":           {"required": ["name"]},
    "collect":        {"required": ["targetPos"]},
}

# 需要	targetPos 的动作（校验器语义层用）
POS_ACTIONS = {"move", "build", "remove", "collect", "summonTreasure", "attack"}

# 建造物名称（build.name 合法取值）
BUILDABLE = {"gatling", "railgun", "rocket", "wall"}

# 角色类型
ROLE_TYPES = {"station", "gatling", "railgun", "rocket", "wall", "pioneer", "worker"}
ROBOT_TYPES = {"smallRobot", "middleRobot", "largeRobot", "bossRobot"}

# 武器工事类型
WEAPON_TYPES = {"gatling", "railgun", "rocket"}

# ---- 昼夜时间公式（任务书 4.2）----
DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS  # 130
MAX_ROUNDS = 1300
MAX_DAYS = 10


def day_of(round_no: int) -> int:
    """游戏日，1-based。"""
    return (round_no - 1) // ROUNDS_PER_DAY + 1


def phase_of(round_no: int) -> str:
    """返回 'day' 或 'night'。"""
    r = (round_no - 1) % ROUNDS_PER_DAY
    return "day" if r < DAY_ROUNDS else "night"


def is_day_first_round(round_no: int) -> bool:
    """白天第一个回合（新闻发布/配额重置）。"""
    return (round_no - 1) % ROUNDS_PER_DAY == 0


def is_night_first_round(round_no: int) -> bool:
    """夜晚第一个回合（机器人出现）。"""
    return (round_no - 1) % ROUNDS_PER_DAY == DAY_ROUNDS


def is_day_last_round(round_no: int) -> bool:
    return (round_no - 1) % ROUNDS_PER_DAY == DAY_ROUNDS - 1


# ---- 物品（《接口文档》2.3 / 任务书 4.6.3）----
WEAPON_SHOP_ITEMS = {
    "WeaponUpgradeVoucher1": 100,
    "WeaponUpgradeVoucher2": 150,
    "WallUpgradeVoucher1": 20,
    "WallUpgradeVoucher2": 30,
    "StationUpgradeVoucher1": 100,
    "StationUpgradeVoucher2": 150,
    "WallFixer": 10,
    "Medicine": 10,
    "DizzyWeapon": 100,
    "Bomb": 100,
    "SmallRobotSummonOrder": 20,
    "MiddleRobotSummonOrder": 30,
    "LargeRobotSummonOrder": 100,
    "BossRobotSummonOrder": 200,
    "AcientTablet": 15,
    "StarSand": 15,
    "FlameBreath": 15,
    "FrostPotion": 15,
    "ThornAmulet": 15,
    "IronWhistle": 15,
}

UPGRADE_VOUCHERS = {
    "WeaponUpgradeVoucher1": ("weapon", 1, 2),
    "WeaponUpgradeVoucher2": ("weapon", 2, 3),
    "WallUpgradeVoucher1": ("wall", 1, 2),
    "WallUpgradeVoucher2": ("wall", 2, 3),
    "StationUpgradeVoucher1": ("station", 1, 2),
    "StationUpgradeVoucher2": ("station", 2, 3),
}

SUMMON_ORDERS = {
    "SmallRobotSummonOrder": "smallRobot",
    "MiddleRobotSummonOrder": "middleRobot",
    "LargeRobotSummonOrder": "largeRobot",
    "BossRobotSummonOrder": "bossRobot",
}

ORES = ("stone", "iron", "copper")
ORES_CN = {"stone": "石", "iron": "铁", "copper": "铜"}
TASK_ITEMS = ("AcientTablet", "StarSand", "FlameBreath", "FrostPotion", "ThornAmulet", "IronWhistle")

# 需要 targetPos 才能使用的物品（任务书 4.6.3 注）
USE_NEEDS_POS = {"WallFixer", "DizzyWeapon", "Bomb",
                 "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
                 "WallUpgradeVoucher1", "WallUpgradeVoucher2",
                 "StationUpgradeVoucher1", "StationUpgradeVoucher2"}

# ---- 角色属性（任务书 4.5）----
ROLE_HP = {"pioneer": 200, "worker": 220, "station": 1500,
           "gatling": 1000, "railgun": 1000, "rocket": 1000, "wall": 1000}
ROLE_LEVEL_HP = {  # 等级 -> 满血
    "station": {1: 1500, 2: 3000, 3: 4500},
    "gatling": {1: 1000, 2: 1500, 3: 2000},
    "railgun": {1: 1000, 2: 1500, 3: 2000},
    "rocket": {1: 1000, 2: 1500, 3: 2000},
    "wall": {1: 1000, 2: 1500, 3: 2000},
}
BACKPACK_CAP = {"pioneer": 40, "worker": 100}
WEAPON_BUILD_COST = 25
MAX_WEAPONS = 3
ROBOT_DAILY_SUMMON_LIMIT = 10

# 机器人属性（任务书 4.7.2）
ROBOT_STATS = {
    "smallRobot":  {"hp": 40, "atk": 5, "score": 1},
    "middleRobot": {"hp": 60, "atk": 10, "score": 2},
    "largeRobot":  {"hp": 500, "atk": 20, "score": 4},
    "bossRobot":   {"hp": 800, "atk": 40, "score": 10},
}

# 角色初始 ID（《接口文档》1.3.1 分配表）
TEAM_IDS = {
    "challenger": {"worker1": 10010, "pioneer": 10011, "worker2": 10012, "station": 10013,
                   "gatling": [10020, 10021, 10022], "railgun": [10030, 10031, 10032],
                   "rocket": [10040, 10041, 10042], "wall_base": 40000},
    "defender":   {"worker1": 20010, "pioneer": 20011, "worker2": 20012, "station": 20013,
                   "gatling": [20020, 20021, 20022], "railgun": [20030, 20031, 20032],
                   "rocket": [20040, 20041, 20042], "wall_base": 41000},
}
ROBOT_ID_BASE = 30000
VISION_RANGE = 4


def chebyshev(a, b) -> int:
    return max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"]))


def adjacent(a, b) -> bool:
    """切比雪夫距离 ≤1（含重合）。"""
    return chebyshev(a, b) <= 1


def in_map(p, width=41, height=32) -> bool:
    return 0 <= p["x"] < width and 0 <= p["y"] < height
