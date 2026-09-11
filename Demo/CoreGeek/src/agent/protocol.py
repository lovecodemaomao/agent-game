from dataclasses import dataclass, field
from typing import Any

DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS

WEAPON_BUILD_COST = 25
WALL_MATERIAL = "stone"
LAND = "land"
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
TOWER_TYPES = ("gatling", "railgun", "rocket")
CONTROLLABLE_TYPES = (WORKER, PIONEER)
TOWER_RANGE_BY_LEVEL = {
    "gatling": (3, 5, 7),
    "railgun": (6, 8, 10),
    "rocket": (10, 15, 10**9),
}
# 升级券与修复（任务书 4.6.3）
WALL_FIXER = "WallFixer"
WALL_FIXER_PRICE = 10
WALL_REPAIR_HP = 200          # 围墙血量低于该值优先修复
WEAPON_VOUCHER_BY_LEVEL = {1: "WeaponUpgradeVoucher1", 2: "WeaponUpgradeVoucher2"}
WALL_VOUCHER_BY_LEVEL = {1: "WallUpgradeVoucher1", 2: "WallUpgradeVoucher2"}
STATION_VOUCHER_BY_LEVEL = {1: "StationUpgradeVoucher1", 2: "StationUpgradeVoucher2"}


@dataclass(frozen=True, slots=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos":
        return cls(int(raw["x"]), int(raw["y"]))

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


def distance(first: Pos, second: Pos) -> int:
    return max(abs(first.x - second.x), abs(first.y - second.y))


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    return (
        pos,
        Pos(pos.x + 1, pos.y),
        Pos(pos.x, pos.y - 1),
        Pos(pos.x + 1, pos.y - 1),
    )


@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: int
    pos: Pos
    kind: str
    health: int
    level: int
    cooldown: int
    attack_range: int
    capacity: int | None
    backpack: tuple[str, ...]

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Unit":
        raw_capacity = raw.get("backPackCapability")
        return cls(
            int(raw.get("id") or 0),
            Pos.load(raw["pos"]),
            str(raw["roleType"]),
            int(raw["health"]),
            int(raw.get("level") or 0),
            int(raw.get("cooldown") or 0),
            int(raw.get("attackRange") or 0),
            int(raw_capacity) if raw_capacity is not None else None,
            tuple(str(item) for item in raw.get("backpack") or ()),
        )

    @property
    def backpack_full(self) -> bool:
        if self.capacity is None:
            return False
        return len(self.backpack) >= self.capacity

    def range_of_attack(self) -> int:
        if self.attack_range > 0:
            return self.attack_range
        table = TOWER_RANGE_BY_LEVEL.get(self.kind)
        if table is None:
            return 0
        level = min(max(self.level, 1), len(table))
        return table[level - 1]


@dataclass(frozen=True, slots=True)
class Robot:
    robot_id: int
    pos: Pos
    health: int
    kind: str = "smallRobot"
    target_team: str = ""
    abnormal_state: str = ""

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Robot":
        return cls(int(raw["id"]), Pos.load(raw["pos"]), int(raw["health"]),
                   str(raw.get("roleType", "smallRobot")),
                   str(raw.get("targetTeam", "")), str(raw.get("abnormalState", "")))


@dataclass(frozen=True, slots=True)
class TaskPoint:
    kind: str
    pos: Pos
    cold_down: int
    is_valid: bool
    timeout_rounds: int
    score_reward: int = 0
    gold_reward: int = 0

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "TaskPoint":
        return cls(
            str(raw.get("taskType") or ""),
            Pos.load(raw["taskPosition"]),
            int(raw.get("coldDownRounds") or 0),
            bool(raw.get("isValid")),
            int(raw.get("timeoutRounds") or 0),
            int(raw.get("scoreReward") or 0),
            int(raw.get("goldReward") or 0),
        )


@dataclass(frozen=True, slots=True)
class Turn:
    round_no: int
    is_day: bool
    gold: int
    width: int
    height: int
    zones: dict[Pos, str]
    ours: tuple[Unit, ...]
    robots: tuple[Robot, ...]
    enemies: tuple[Unit, ...] = ()
    team_type: str = ""
    # 任务与沙盒回填（接口文档 1.1）
    tasks: tuple[TaskPoint, ...] = ()
    phase_task: str = ""
    last_cmd_result: str = ""
    llm_resp: str = ""
    last_results: dict[int, bool] = field(default_factory=dict)

    @property
    def round_of_day(self) -> int:
        return (self.round_no - 1) % ROUNDS_PER_DAY + 1

    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Turn":
        round_no = int(payload["roundNo"])
        info = payload["mapInfo"]
        team = payload["teamOur"]
        return cls(
            round_no,
            (round_no - 1) % ROUNDS_PER_DAY < DAY_ROUNDS,
            int(team.get("goldNum") or 0),
            int(info["width"]),
            int(info["height"]),
            {
                Pos.load(zone["pos"]): str(zone["neutralType"])
                for zone in info.get("zones") or ()
            },
            tuple(Unit.load(role) for role in team.get("roles") or ()),
            tuple(
                Robot.load(robot)
                for robot in (payload.get("robot") or {}).get("roles") or ()
            ),
            tuple(Unit.load(role) for role in (payload.get("teamEnemy") or {}).get("roles") or ()),
            str(team.get("type", "")),
            tuple(TaskPoint.load(t) for t in team.get("playerTasks") or ()),
            str(payload.get("phaseTask") or ""),
            str(payload.get("lastCmdResult") or ""),
            str(payload.get("llmResp") or ""),
            {int(k): bool(v) for k, v in (payload.get("lastRoundRoleActionResults") or {}).items()},
        )

    def station(self) -> Unit | None:
        for unit in self.ours:
            if unit.kind == STATION:
                return unit
        return None

    def alive(self, kinds: tuple[str, ...]) -> tuple[Unit, ...]:
        return tuple(
            unit for unit in self.ours
            if unit.kind in kinds and unit.health > 0
        )

    def controllable(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(CONTROLLABLE_TYPES), key=lambda unit: unit.unit_id,
        ))

    def workers(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive((WORKER,)), key=lambda unit: unit.unit_id,
        ))

    def weapons(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(TOWER_TYPES),
            key=lambda unit: (unit.pos.x, unit.pos.y),
        ))

    def walls(self) -> tuple[Unit, ...]:
        return self.alive((WALL,))

    def pioneer(self) -> Unit | None:
        ps = self.alive((PIONEER,))
        return ps[0] if ps else None

    def mines(self) -> tuple[Pos, ...]:
        return tuple(
            pos for pos, kind in self.zones.items()
            if kind in ("stone", "iron", "copper")
        )

    def vendors(self) -> tuple[Pos, ...]:
        return tuple(pos for pos, kind in self.zones.items() if kind == "vendor")

    def shops(self) -> tuple[Pos, ...]:
        return tuple(pos for pos, kind in self.zones.items() if kind == "weaponShop")

    def task_points(self) -> tuple[Pos, ...]:
        return tuple(sorted(
            (t.pos for t in self.tasks if t.is_valid),
            key=lambda p: (p.x, p.y),
        ))

    def stone_mines(self) -> tuple[Pos, ...]:
        return tuple(
            pos for pos, kind in self.zones.items() if kind == WALL_MATERIAL
        )

    def footprint(self, unit: Unit) -> tuple[Pos, ...]:
        if unit.kind == STATION:
            return station_footprint(unit.pos)
        return (unit.pos,)

    def land(self, pos: Pos) -> bool:
        if not 0 <= pos.x < self.width or not 0 <= pos.y < self.height:
            return False
        return self.zones.get(pos, LAND) == LAND

    def occupied_cells(self) -> frozenset[Pos]:
        cells: set[Pos] = set()
        for unit in self.ours + self.enemies:
            if unit.health > 0:
                cells.update(self.footprint(unit))
        return frozenset(cells)

    def blocked(self, moving: Unit) -> frozenset[Pos]:
        cells = {pos for pos, kind in self.zones.items() if kind != LAND}
        cells.update(self.occupied_cells())
        cells.discard(moving.pos)
        for robot in self.robots:
            cells.add(robot.pos)
        return frozenset(cells)


def move_command(pos: Pos) -> dict[str, Any]:
    return {"action": "move", "targetPos": [pos.dump()]}


def collect_command(pos: Pos) -> dict[str, Any]:
    return {"action": "collect", "targetPos": [pos.dump()]}


def build_command(pos: Pos, name: str) -> dict[str, Any]:
    return {"action": "build", "targetPos": [pos.dump()], "name": name}


def attack_command(controller_id: int, pos: Pos) -> dict[str, Any]:
    return {
        "action": "attack",
        "targetPos": [pos.dump()],
        "controllerId": str(controller_id),
    }


def sell_command(name: str, num: int) -> dict[str, Any]:
    return {"action": "sell", "name": name, "num": int(num)}


def buy_command(name: str, num: int = 1) -> dict[str, Any]:
    return {"action": "buy", "name": name, "num": int(num)}


def use_command(name: str, pos: Pos | None = None) -> dict[str, Any]:
    command: dict[str, Any] = {"action": "use", "name": name}
    if pos is not None:
        command["targetPos"] = [pos.dump()]
    return command


def accept_task_command() -> dict[str, Any]:
    return {"action": "acceptTask"}


def submit_answer_command(answer: str) -> dict[str, Any]:
    return {"action": "submitAnswer", "taskAnswer": answer}
