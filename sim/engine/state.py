# -*- coding: utf-8 -*-
"""引擎世界状态与初始化（任务4.1）。

单位模型：角色/建筑/机器人统一为 Unit，team ∈ {challenger, defender, robot}。
坐标 {x, y}，切比雪夫距离。矿区 Mine 含剩余量。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from common import rules as R
from sim.engine.config import EngineConfig


@dataclass
class Unit:
    id: int
    team: str                 # challenger / defender / robot
    pos: Dict[str, int]
    role_type: str
    hp: int
    level: int = 1
    cooldown: int = 0
    backpack: List[str] = field(default_factory=list)
    alive: bool = True
    abnormal: str = ""        # 机器人 dizzy
    revive_at: int = 0        # 角色复活回合
    # 机器人
    target_team: str = ""
    atk: int = 0

    def to_role_json(self, include_backpack: bool = True) -> Dict:
        d = {
            "id": self.id, "pos": dict(self.pos), "roleType": self.role_type,
            "health": self.hp, "attackPower": self.atk if self.team == "robot" else self.attack_power(),
            "attackRange": self.attack_range(),
            "level": self.level, "cooldown": self.cooldown,
        }
        if include_backpack:
            d["backPackCapability"] = R.BACKPACK_CAP.get(self.role_type, 0)
            d["backpack"] = list(self.backpack)
        return d

    def attack_power(self) -> int:
        """单发攻击力：加特林10/颗，电磁 10*level，火箭 20/枚。"""
        if self.role_type == "gatling":
            return 10
        if self.role_type == "railgun":
            return 10 * self.level
        if self.role_type == "rocket":
            return 20
        return 0

    def shot_count(self) -> int:
        """每回合弹数/导弹数 = 等级（加特林/火箭）。"""
        return self.level if self.role_type in ("gatling", "rocket") else 1

    def attack_range(self) -> int:
        if self.role_type == "gatling":
            return (1, 3, 5, 7)[self.level]
        if self.role_type == "railgun":
            return (6, 8, 10)[self.level - 1]
        if self.role_type == "rocket":
            return (10, 15, 2147483647)[self.level - 1]
        return 0

    def max_hp(self) -> int:
        return R.ROLE_LEVEL_HP[self.role_type][self.level]


@dataclass
class Mine:
    pos: Dict[str, int]
    kind: str                 # stone / iron / copper
    remaining: int = 10
    refresh_at: Optional[int] = None  # 枯竭后待刷新回合


@dataclass
class TaskPointState:
    team: str                 # 所属阵营
    point_id: str             # point1 / point2
    positions: List[Dict[str, int]]
    cold_down: int = 0
    remaining_total: int = 5  # 任务总数上限
    active_task: Optional[Dict] = None   # {start_round, timeout_rounds, desc, best_answer, best_rate, city...}
    score_reward: int = 50
    gold_reward: int = 30
    timeout_rounds: int = 100


@dataclass
class TeamState:
    team: str
    gold: int = 75
    score_task: float = 0.0   # score1
    score_kill: int = 0       # score2
    score_survive: float = 0.0
    summons_used_today: int = 0
    summon_orders_pending: List[str] = field(default_factory=list)  # 下夜生效
    pending_task_rewards: List[Dict] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return round(self.score_task + self.score_kill + self.score_survive, 1)


@dataclass
class WorldState:
    round_no: int = 0
    width: int = 41
    height: int = 32
    units: Dict[int, Unit] = field(default_factory=dict)
    mines: List[Mine] = field(default_factory=list)
    teams: Dict[str, TeamState] = field(default_factory=dict)
    task_points: Dict[str, List[TaskPointState]] = field(default_factory=dict)  # team -> [point1, point2]
    zones: List[Dict] = field(default_factory=list)      # 静态中立（任务点/小贩/商店）
    news_prices: Dict[str, float] = field(default_factory=dict)  # 当前矿石收购价
    news_mine_blocked: Dict[str, int] = field(default_factory=dict)  # 矿种 -> 剩余停采回合
    treasure: Dict = field(default_factory=dict)         # 宝藏谜题（组8填充）
    treasure_taken: bool = False
    last_results: Dict[str, Dict[int, bool]] = field(default_factory=dict)  # team -> {roleId: legal}
    last_summon_result: Dict[str, int] = field(default_factory=dict)
    base_destroyed_round: Dict[str, Optional[int]] = field(default_factory=dict)  # team -> 摧毁回合
    last_cmd_result: Dict[str, str] = field(default_factory=dict)
    llm_resp: Dict[str, str] = field(default_factory=dict)
    world_news: Dict[str, str] = field(default_factory=dict)
    phase_task: Dict[str, str] = field(default_factory=dict)
    events: List[Dict] = field(default_factory=list)     # 本回合事件（前端/记录用）
    robot_id_seq: int = R.ROBOT_ID_BASE
    wall_seq: Dict[str, int] = field(default_factory=dict)
    # 结算辅助
    pending_damage: Dict[int, int] = field(default_factory=dict)  # unit_id -> 累计伤害
    moved_this_round: Set[int] = field(default_factory=set)
    winner: Optional[str] = None

    # ------------------------------------------------ 初始化
    @classmethod
    def create(cls, cfg: EngineConfig, seed: int = 42) -> "WorldState":
        rng = random.Random(seed)
        w = cls()
        w.width, w.height = cfg.width, cfg.height
        w.teams = {t: TeamState(team=t, gold=cfg.economy["initial_gold"]) for t in ("challenger", "defender")}
        for t in ("challenger", "defender"):
            w.wall_seq[t] = R.TEAM_IDS[t]["wall_base"]
            w.base_destroyed_round[t] = None
        w.news_prices = dict(cfg.economy["base_prices"])  # 世界级矿价
        w.news_mine_blocked = {o: 0 for o in R.ORES}
        w.round_no = 0
        w._init_units(cfg)
        w._init_zones(cfg)
        w._init_mines(cfg, rng)
        return w

    def _init_units(self, cfg: EngineConfig) -> None:
        for team in ("challenger", "defender"):
            ids = R.TEAM_IDS[team]
            bx, by = cfg.base_pos(team)
            # 基地 2x2，pos 为左上角
            self.units[ids["station"]] = Unit(ids["station"], team, {"x": bx, "y": by}, "station",
                                              hp=R.ROLE_LEVEL_HP["station"][1])
            # 武器工事初始数量为0（任务书4.5.1），需工人白天建造；ID 按分配表预留
            # 角色
            self.units[ids["worker1"]] = Unit(ids["worker1"], team, {"x": bx - 3, "y": by - 1}, "worker",
                                              hp=R.ROLE_HP["worker"])
            self.units[ids["worker2"]] = Unit(ids["worker2"], team, {"x": bx - 3, "y": by - 2}, "worker",
                                              hp=R.ROLE_HP["worker"])
            self.units[ids["pioneer"]] = Unit(ids["pioneer"], team, {"x": bx - 3, "y": by - 3}, "pioneer",
                                              hp=R.ROLE_HP["pioneer"])

    def _init_zones(self, cfg: EngineConfig) -> None:
        tp = cfg.task_points
        z: List[Dict] = []
        for team, prefix in (("challenger", "challengerTaskPoint"), ("defender", "defenderTaskPoint")):
            for pid, positions in tp[team].items():
                num = pid.replace("point", "")
                for p in positions:
                    z.append({"neutralType": f"{prefix}{num}", "pos": {"x": p[0], "y": p[1]}})
        z.append({"neutralType": "vendor", "pos": {"x": cfg.vendor[0], "y": cfg.vendor[1]}})
        z.append({"neutralType": "weaponShop", "pos": {"x": cfg.weapon_shop[0], "y": cfg.weapon_shop[1]}})
        self.zones = z
        # 任务点状态
        for team in ("challenger", "defender"):
            pts = []
            for pid, positions in tp[team].items():
                pts.append(TaskPointState(
                    team=team, point_id=pid, positions=[{"x": p[0], "y": p[1]} for p in positions],
                    remaining_total=cfg.tasks["per_point_total"],
                    score_reward=cfg.tasks["score_reward"], gold_reward=cfg.tasks["gold_reward"],
                    timeout_rounds=cfg.tasks["timeout_rounds"]))
            self.task_points[team] = pts

    def _init_mines(self, cfg: EngineConfig, rng: random.Random) -> None:
        self.mines = []
        for kind, n in cfg.mine_count.items():
            for _ in range(n):
                self.mines.append(Mine(pos=self._random_free_cell(rng), kind=kind,
                                       remaining=cfg.economy["mine_capacity"]))

    # ------------------------------------------------ 查询
    def base_units(self, team: str) -> List[Unit]:
        return [u for u in self.units.values() if u.team == team and u.role_type == "station" and u.alive]

    def cells_of(self, u: Unit) -> List[Tuple[int, int]]:
        """单位占据的格子（基地2x2，其余1x1）。

        接口文档口径: 基地 pos 为左上角，向右下延伸（官方 demo station_footprint 同款）。
        """
        if u.role_type == "station":
            x, y = u.pos["x"], u.pos["y"]
            return [(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)]
        return [(u.pos["x"], u.pos["y"])]

    def occupied(self, exclude: Optional[Set[int]] = None) -> Dict[Tuple[int, int], int]:
        occ = {}
        for u in self.units.values():
            if not u.alive or (exclude and u.id in exclude):
                continue
            for c in self.cells_of(u):
                occ[c] = u.id
        for m in self.mines:
            occ[(m.pos["x"], m.pos["y"])] = -1
        for z in self.zones:
            occ[(z["pos"]["x"], z["pos"]["y"])] = -1
        return occ

    def zone_distance(self, pos: Dict[str, int], team: str) -> int:
        bu = [u for u in self.units.values() if u.team == team and u.role_type == "station" and u.alive]
        if not bu:
            return 999
        b = bu[0]
        x0, y0 = b.pos["x"], b.pos["y"]
        cells = [(x0, y0), (x0 + 1, y0), (x0, y0 - 1), (x0 + 1, y0 - 1)]  # 左上角向右下
        return min(max(abs(pos["x"] - cx), abs(pos["y"] - cy)) for cx, cy in cells)

    def _random_free_cell(self, rng: random.Random) -> Dict[str, int]:
        for _ in range(500):
            x = rng.randrange(self.width)
            y = rng.randrange(self.height)
            p = {"x": x, "y": y}
            if (x, y) in self.occupied():
                continue
            if self.zone_distance(p, "challenger") <= 6 or self.zone_distance(p, "defender") <= 6:
                continue
            if any(z["pos"] == p for z in self.zones):
                continue
            return p
        return {"x": rng.randrange(self.width), "y": rng.randrange(self.height)}

    # ------------------------------------------------ 视野（4.2 详细版在 view.py）
    def visible_enemy_ids(self, team: str) -> Set[int]:
        enemy = "defender" if team == "challenger" else "challenger"
        own = [u for u in self.units.values() if u.team == team and u.alive]
        vis: Set[int] = set()
        for u in self.units.values():
            if u.team != enemy or not u.alive:
                continue
            if u.role_type in ("station", "wall"):
                vis.add(u.id)
                continue
            for o in own:
                if R.chebyshev(o.pos, u.pos) <= R.VISION_RANGE:
                    vis.add(u.id)
                    break
        return vis

    def alive_robots(self) -> List[Unit]:
        return [u for u in self.units.values() if u.team == "robot" and u.alive]

    def team_characters(self, team: str) -> List[Unit]:
        return [u for u in self.units.values() if u.team == team and u.alive
                and u.role_type in ("pioneer", "worker")]

    def team_weapons(self, team: str) -> List[Unit]:
        return [u for u in self.units.values() if u.team == team and u.alive
                and u.role_type in R.WEAPON_TYPES]

    def next_robot_id(self) -> int:
        self.robot_id_seq += 1
        return self.robot_id_seq

    def emit(self, etype: str, **kw) -> None:
        ev = {"type": etype, "round": self.round_no}
        ev.update(kw)
        self.events.append(ev)
