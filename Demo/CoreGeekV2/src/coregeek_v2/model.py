"""Contracts shared by scheduling, jobs, feedback and execution."""
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from .world import Pos, FrozenMap


class Priority(IntEnum):
    EMERGENCY = 0
    NIGHT_PREP = 1
    CRITICAL_DEFENSE = 2
    NORMAL = 3
    OPTIONAL = 4


class Lifecycle(str, Enum):
    CREATED = 'CREATED'
    ACTIVE = 'ACTIVE'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    PREEMPTED = 'PREEMPTED'
    CANCELLED = 'CANCELLED'


class Signal(str, Enum):
    CONTINUE = 'CONTINUE'
    SUCCESS = 'SUCCESS'
    FAIL = 'FAIL'
    NEEDS_REBIND = 'NEEDS_REBIND'
    RETRY = 'RETRY'


@dataclass(frozen=True)
class ReservationRequest:
    gold: float = 0
    keys: tuple = ()


@dataclass(frozen=True)
class Reservation:
    owner_job_id: str
    gold: float
    keys: tuple


@dataclass(frozen=True)
class JobProposal:
    kind: str
    semantic_key: str
    eligible_roles: tuple
    source: str
    priority: Priority = Priority.NORMAL
    utility: float = 0
    target: Pos | None = None
    deadline: int | None = None
    estimated_duration: int = 1
    reservation: ReservationRequest = ReservationRequest()
    interruptible: bool = True
    fallback_targets: tuple = ()
    data: FrozenMap = field(default_factory=FrozenMap)


@dataclass(frozen=True)
class MoveIntent:
    goal: Pos


@dataclass(frozen=True)
class ActionIntent:
    action: str
    params: FrozenMap = field(default_factory=FrozenMap)


class Job:
    """Execution behavior only; scheduler-owned metadata lives in JobRecord."""
    def __init__(self):
        self.stage = 'START'

    def on_outcome(self, outcome):
        return Signal.CONTINUE

    def check(self, world, role_id, binding):
        return Signal.CONTINUE

    def intent(self, world, role_id, binding):
        return None

    def remaining_duration(self, world, role_id, binding, navigation):
        """Complete remaining chain, including actions and return, in turns.

        Default supports endpoint-only jobs. Business jobs must override for
        interaction squares and multi-stage trips; binding may be an obstacle.
        """
        return navigation.cost(world, world.role(role_id).pos, binding) if binding is not None else 1

    def reservation_request(self, world, role_id, binding, current):
        """Request unspent budget after feedback; never mutate the ledger."""
        return current


@dataclass(frozen=True)
class JobRecord:
    id: str
    role_id: int
    proposal: JobProposal
    behavior: Job
    binding: Pos | None
    lifecycle: Lifecycle = Lifecycle.ACTIVE


@dataclass(frozen=True)
class Tombstone:
    semantic_key: str
    role_id: int
    reason: str
    round_no: int
    cooldown_until: int


@dataclass(frozen=True)
class DayPlan:
    version: int
    day_no: int
    stone_target: int = 0
    gold_reserve: float = 0
    defense_pressure: float = 0
    economy_priority: float = 0
    task_priority: float = 0
    upgrade_policy: str = 'disabled'
    night_prep_policy: str = 'day_70'
    required_builds: tuple = ()
    desired_upgrades: tuple = ()
    required_repairs: tuple = ()
    gold_target: float = 0
    posts: FrozenMap = field(default_factory=FrozenMap)


@dataclass
class GameMemory:
    outcomes: list = field(default_factory=list)
    mine_used: dict = field(default_factory=dict)
    mine_kinds: dict = field(default_factory=dict)
    wall_damage: dict = field(default_factory=dict)
    news: list = field(default_factory=list)
    advice: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    treasure_attempts: set = field(default_factory=set)
    failures: dict = field(default_factory=dict)
    seen_jobs: set = field(default_factory=set)


class GameMemoryReducer:
    def update(self, memory, outcomes):
        memory.outcomes.extend(outcomes)
        memory.outcomes[:] = memory.outcomes[-300:]

    def observe(self, memory, world, previous):
        mines = {p: k for p, k in world.zones.items() if k in ('stone', 'iron', 'copper')}
        for pos in tuple(memory.mine_used):
            if pos not in mines or mines[pos] != memory.mine_kinds.get(pos):
                memory.mine_used.pop(pos, None)
        memory.mine_kinds = mines
        news = world.payload.get('worldNews')
        if news and news not in memory.news:
            memory.news.append(news)
        if previous:
            for building in previous.alive(('wall', 'station')):
                current = world.role(building.id)
                lost = building.health - (current.health if current else 0)
                if lost > 0:
                    memory.wall_damage[building.pos] = memory.wall_damage.get(building.pos, 0) + lost
        for outcome in memory.outcomes:
            if outcome.round_no != world.round_no - 1:
                continue
            cmd = outcome.evidence.get('command', {})
            if outcome.kind == 'COLLECT_SUCCESS':
                pos = Pos.load(cmd['targetPos'][0])
                memory.mine_used[pos] = memory.mine_used.get(pos, 0) + 1
            if cmd.get('action') == 'summonTreasure' and outcome.kind != 'UNCONFIRMED':
                memory.treasure_attempts.add(Pos.load(cmd['targetPos'][0]))

    def skill(self, memory, text, method):
        memory.skills[text] = method

    def reconcile_jobs(self, memory, state, world):
        for record in state.jobs.values():
            if record.lifecycle in (Lifecycle.ACTIVE, Lifecycle.CREATED) or record.id in memory.seen_jobs:
                continue
            memory.seen_jobs.add(record.id)
            if record.lifecycle == Lifecycle.FAILED:
                memory.failures[record.proposal.semantic_key] = world.round_no + 4
            if record.lifecycle == Lifecycle.COMPLETED and hasattr(record.behavior, 'learning'):
                text, method = record.behavior.learning()
                self.skill(memory, text, method)

    def accept_advice(self, memory, world, value):
        import json
        from .execution import thaw
        text = '\n'.join(json.dumps(thaw(n), ensure_ascii=False) for n in memory.news)
        blocked, holds = [], []
        blocked_input = value.get('blocked_mines')
        holds_input = value.get('hold_ores')
        for entry in blocked_input if isinstance(blocked_input, list) else ():
            if not isinstance(entry, dict):
                continue
            if (entry.get('kind') in ('stone','iron','copper') and type(entry.get('start_day')) is int
                    and type(entry.get('end_day')) is int and 1 <= entry['start_day'] <= entry['end_day'] <= 10
                    and isinstance(entry.get('evidence'), str) and entry['evidence'] and entry['evidence'] in text):
                blocked.append(entry)
        for entry in holds_input if isinstance(holds_input, list) else ():
            if (isinstance(entry,dict) and entry.get('kind') in ('stone','iron','copper')
                    and type(entry.get('until_day')) is int and world.day_no < entry['until_day'] <= 10
                    and isinstance(entry.get('evidence'),str) and entry['evidence'] and entry['evidence'] in text):
                holds.append(entry)
        advice = {'blocked_mines':blocked, 'hold_ores':holds}
        treasure = value.get('treasure')
        if isinstance(treasure, dict):
            try:
                pos, start, end = treasure['pos'], treasure['start_round'], treasure['end_round']
                names = {p['name'] for p in world.weapon_shop_prices}
                evidence = treasure['evidence']
                if (type(pos['x']) is int and type(pos['y']) is int and 0 <= pos['x'] < world.width
                        and 0 <= pos['y'] < world.height and type(start) is int and type(end) is int
                        and 1 <= start <= end <= 1300 and isinstance(treasure['items'], list) and treasure['items']
                        and all(isinstance(i,str) and i in names for i in treasure['items'])
                        and float(treasure.get('confidence',0)) >= .9 and isinstance(evidence,str) and evidence and evidence in text):
                    advice['treasure'] = treasure
            except (KeyError, TypeError, ValueError):
                pass
        # A missing new inference does not erase a still useful old treasure.
        if 'treasure' not in advice and memory.advice.get('treasure'):
            advice['treasure'] = memory.advice['treasure']
        memory.advice = advice


@dataclass
class RuntimeState:
    jobs: dict = field(default_factory=dict)
    assignments: dict = field(default_factory=dict)
    reservations: dict = field(default_factory=dict)
    tombstones: list = field(default_factory=list)
    previous_actions: tuple = ()
    previous_world: object = None
    current_day_plan: DayPlan | None = None
    sequence: int = 0
    auxiliary: dict = field(default_factory=dict)


class StrategicPlanner:
    def plan(self, world, memory, previous):
        if previous and previous.day_no == world.day_no:
            return previous
        return DayPlan((previous.version + 1) if previous else 1, world.day_no)
