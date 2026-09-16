"""Transactional per-team, idempotent turn pipeline."""
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from .world import WorldParser, freeze
from .model import GameMemory, RuntimeState, GameMemoryReducer, StrategicPlanner, Lifecycle
from .scheduler import Scheduler
from .navigation import Navigation, MovementCoordinator
from .feedback import OutcomeBuilder, Recorder
from .execution import Executor, Validator, TaskExecution, ResponseBuilder

LOG = logging.getLogger(__name__)


@dataclass
class Session:
    memory: GameMemory = field(default_factory=GameMemory)
    runtime: RuntimeState = field(default_factory=RuntimeState)
    round_no: int = 0
    response: dict | None = None


class Agent:
    def __init__(self, planners=(), factories=None, development=False, strategy=None, production=False):
        self.sessions = {}
        self.lock = RLock()
        self.planners = tuple(planners)
        self.scheduler = Scheduler(factories)
        self.navigation = Navigation()
        self.development = development
        self.strategy = strategy or StrategicPlanner()
        self.production_mode = production

    @classmethod
    def production(cls, development=False):
        from .jobs import FACTORIES
        from .task_solver import TaskJob
        from .strategy import (DailyStrategy, ConstructionPlanner, EconomyPlanner, DefensePlanner,
                               TaskPlanner, TreasurePlanner, NightPrepPlanner)
        nav = Navigation()
        agent = cls([kind(nav) for kind in (ConstructionPlanner, TaskPlanner, DefensePlanner,
                                            TreasurePlanner, EconomyPlanner, NightPrepPlanner)],
                    dict(FACTORIES, Task=TaskJob), development, DailyStrategy(), True)
        agent.navigation = nav
        return agent

    def respond(self, payload):
        with self.lock:
            try:
                world = WorldParser().parse(payload)
                current = self.sessions.get(world.team_key)
                if current and current.round_no == world.round_no:
                    return deepcopy(current.response)
                working = deepcopy(current) if current and current.round_no < world.round_no else Session()
                response = self.turn(world, working)
                working.round_no, working.response = world.round_no, deepcopy(response)
                self.sessions[world.team_key] = working
                return response
            except Exception:
                if self.development:
                    raise
                LOG.exception('turn failed; committed state preserved')
                return ResponseBuilder().build({}, ('', ''))

    def turn(self, world, session):
        state = session.runtime
        outcomes = OutcomeBuilder().build(state.previous_world, state.previous_actions, world)
        GameMemoryReducer().update(session.memory, outcomes)
        if self.production_mode:
            GameMemoryReducer().observe(session.memory, world, state.previous_world)
            TaskExecution().receive(world, state, session.memory)
            if not world.station():
                self.scheduler.cancel_all(state, world, 'owned station destroyed or absent')
                GameMemoryReducer().reconcile_jobs(session.memory, state, world)
                Recorder().record(state, world, ())
                state.auxiliary.clear()
                return ResponseBuilder().build({}, ('', ''))
        signals = {}
        for outcome in outcomes:
            record = state.jobs.get(outcome.origin_job_id)
            if (record and record.lifecycle == Lifecycle.ACTIVE
                    and state.assignments.get(outcome.role_id) == record.id):
                signals[record.id] = record.behavior.on_outcome(outcome)
        previous_plan = state.current_day_plan
        state.current_day_plan = self.strategy.plan(world, session.memory, previous_plan)
        if state.current_day_plan != previous_plan:
            LOG.info('DAY_PLAN round=%s plan=%s', world.round_no, state.current_day_plan)
        self.scheduler.maintain(state, world, signals, self.navigation)
        if self.production_mode:
            GameMemoryReducer().reconcile_jobs(session.memory, state, world)
        proposals = []
        # Planners receive history and reservations as immutable read views;
        # never the mutable session, scheduler, assignments, or command buffer.
        memory_view = tuple(session.memory.outcomes)
        if self.production_mode:
            memory_view = freeze({key: getattr(session.memory, key) for key in
                                  ('mine_used','wall_damage','news','advice','skills','treasure_attempts','failures')})
        reservation_view = freeze(state.reservations)
        for planner in self.planners:
            proposals.extend(planner.propose(world, memory_view, state.current_day_plan, reservation_view))
        self.scheduler.assign(state, world, proposals, self.navigation)
        if self.production_mode:
            from .combat import prepare_fire
            prepare_fire(world, state)
        intents = []
        for role in world.roles:
            if role.kind in ('worker', 'pioneer') and role.health > 0 and role.id not in state.assignments:
                LOG.info('round=%s role=%s job=none stage=IDLE', world.round_no, role.id)
        for record in sorted(state.jobs.values(), key=lambda r: (r.proposal.priority, r.role_id)):
            if record.lifecycle != Lifecycle.ACTIVE:
                continue
            intent = record.behavior.intent(world, record.role_id, record.binding)
            LOG.info('round=%s role=%s job=%s kind=%s stage=%s source=%s intent=%s outcomes=%s',
                     world.round_no, record.role_id, record.id, record.proposal.kind, record.behavior.stage,
                     record.proposal.source, intent, tuple(o.kind for o in outcomes if o.role_id == record.role_id))
            if intent is not None:
                intents.append((record, intent))
        resolved = MovementCoordinator().resolve(world, intents, self.navigation)
        executor = Executor()
        commands = Validator().validate(world, executor.translate(resolved), self.development, state)
        self.scheduler.assert_invariants(state)
        for cmd in commands:
            LOG.info('round=%s role=%s job=%s command=%s', world.round_no, cmd.role_id, cmd.job_id, cmd.command)
        Recorder().record(state, world, commands)
        auxiliary = TaskExecution().output(world, state, session.memory) if self.production_mode else ('', '')
        return ResponseBuilder().build(executor.command_map(commands), auxiliary)
