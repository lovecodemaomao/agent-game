import ast
import copy
import json
import re
import subprocess
import sys
import threading
import socket
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from coregeek_v2.world import WorldParser, Pos, freeze
from coregeek_v2.model import (Job, JobProposal, Priority, Lifecycle, Signal, MoveIntent,
                               ActionIntent, ReservationRequest, RuntimeState)
from coregeek_v2.scheduler import Scheduler
from coregeek_v2.navigation import Navigation, MovementCoordinator
from coregeek_v2.execution import Executor, Validator, CommandEnvelope
from coregeek_v2.feedback import Outcome, OutcomeBuilder, PreviousActionRecord
from coregeek_v2.runtime import Agent
from coregeek_v2.server import make_server


def role(rid=1, kind='worker', x=1, y=1):
    return dict(id=rid, roleType=kind, pos=dict(x=x, y=y), health=100,
                level=1, backpack=[], backPackCapability=10)


def payload(round_no=1):
    return dict(roundNo=round_no, mapInfo=dict(width=12, height=12, zones=[]),
                teamOur=dict(teamId='team', type='challenger', goldNum=130,
                             roles=[role(), role(2, x=3), role(3, 'pioneer', 5, 1)]),
                teamEnemy=dict(roles=[]), robot=dict(roles=[]),
                lastRoundRoleActionResults={}, lastSummonTreasureResult=0,
                phaseTask='', llmResp='', lastCmdResult='', errors=[])


def proposal(key='mine', **kw):
    return JobProposal(kind='dummy', semantic_key=key, eligible_roles=(1,), source='test', **kw)


class DummyJob(Job):
    def __init__(self, p):
        super().__init__()
        self.stage = 'GO'

    def on_outcome(self, outcome):
        if outcome.kind == 'MOVE_SUCCESS':
            self.stage = 'ARRIVED'
        return Signal.CONTINUE

    def check(self, world, role_id, binding):
        if binding is not None and world.role(role_id).pos == binding:
            return Signal.SUCCESS
        return Signal.CONTINUE

    def intent(self, world, role_id, binding):
        return MoveIntent(binding) if binding else None


class Proposer:
    def __init__(self, proposals):
        self.proposals = proposals

    def propose(self, world, memory, plan, reservations):
        return self.proposals


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.raw = payload()
        self.world = WorldParser().parse(self.raw)
        self.state = RuntimeState()
        self.nav = Navigation()
        self.scheduler = Scheduler({'dummy': DummyJob})

    def assign(self, proposals):
        self.scheduler.assign(self.state, self.world, proposals, self.nav)
        return self.state.jobs[self.state.assignments[1]]

    def test_one_role_one_active_job(self):
        self.assign([proposal('a'), proposal('b')])
        self.assertEqual(len(self.state.assignments), 1)
        self.scheduler.assert_invariants(self.state)

    def test_semantic_deduplication(self):
        self.assign([proposal()])
        self.scheduler.assign(self.state, self.world,
                              [replace(proposal(), eligible_roles=(2,))], self.nav)
        self.assertEqual(len(self.state.jobs), 1)

    def test_world_is_deeply_readonly(self):
        with self.assertRaises(FrozenInstanceError):
            self.world.gold = 0
        with self.assertRaises(TypeError):
            self.world.payload['teamOur']['roles'][0]['pos']['x'] = 8
        self.raw['teamOur']['roles'][0]['pos']['x'] = 9
        self.assertEqual(self.world.roles[0].pos.x, 1)
        self.assertEqual(copy.deepcopy(self.world).payload, self.world.payload)

    def test_completed_failed_cancelled_release_assignment(self):
        for terminal in (Lifecycle.COMPLETED, Lifecycle.FAILED, Lifecycle.CANCELLED):
            with self.subTest(terminal=terminal):
                self.state = RuntimeState()
                rec = self.assign([proposal(reservation=ReservationRequest(100, ('wall:1',)))])
                self.scheduler.finish(self.state, rec, terminal, self.world)
                self.assertEqual(self.state.assignments, {})
                self.assertEqual(self.state.reservations, {})
                self.scheduler.assert_invariants(self.state)

    def test_same_priority_job_does_not_preempt(self):
        rec = self.assign([proposal()])
        self.assign([proposal('richer', utility=10000)])
        self.assertEqual(self.state.assignments[1], rec.id)

    def test_night_prep_can_preempt_economy_and_release_reservation(self):
        old = self.assign([proposal(reservation=ReservationRequest(100, ('gold-plan',)))])
        new = self.assign([proposal('night', priority=Priority.NIGHT_PREP)])
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(self.state.jobs[old.id].lifecycle, Lifecycle.PREEMPTED)
        self.assertNotIn(old.id, self.state.reservations)
        self.assertEqual(self.state.tombstones[0].semantic_key, 'mine')

    def test_preempted_tombstone_blocks_reassignment_today(self):
        self.assign([proposal()])
        new = self.assign([proposal('night', priority=Priority.NIGHT_PREP)])
        self.scheduler.cancel(self.state, new.id, self.world)
        self.scheduler.assign(self.state, self.world, [proposal()], self.nav)
        self.assertNotIn(1, self.state.assignments)
        tomorrow = WorldParser().parse(payload(131))
        self.scheduler.maintain(self.state, tomorrow, {}, self.nav)
        self.scheduler.assign(self.state, tomorrow, [proposal()], self.nav)
        self.assertIn(1, self.state.assignments)

    def test_noninterruptible_job_rejects_emergency_preemption(self):
        old = self.assign([proposal(interruptible=False)])
        self.assign([proposal('emergency', priority=Priority.EMERGENCY)])
        self.assertEqual(self.state.assignments[1], old.id)

    def test_unaffordable_preemption_keeps_original_job(self):
        old = self.assign([proposal()])
        self.assign([proposal('emergency', priority=Priority.EMERGENCY,
                              reservation=ReservationRequest(200))])
        self.assertEqual(self.state.assignments[1], old.id)

    def test_reservation_budget_and_exclusive_target(self):
        self.assign([proposal(reservation=ReservationRequest(100, ('wall:1',)))])
        self.assertEqual(self.scheduler.reservations.available_gold(self.state, self.world), 30)
        for req in (ReservationRequest(40), ReservationRequest(0, ('wall:1',))):
            self.scheduler.assign(self.state, self.world,
                                  [replace(proposal('other', reservation=req), eligible_roles=(2,))], self.nav)
            self.assertNotIn(2, self.state.assignments)

    def test_no_orphan_reservations(self):
        self.state.reservations['ghost'] = ReservationRequest()
        with self.assertRaisesRegex(AssertionError, 'orphan'):
            self.scheduler.assert_invariants(self.state)

    def test_spent_budget_reconciles_before_other_jobs_are_checked(self):
        class PurchaseJob(DummyJob):
            def reservation_request(self, world, role_id, binding, current):
                return ReservationRequest(0, current.keys)
        self.scheduler.factories['purchase'] = PurchaseJob
        first = self.assign([proposal(reservation=ReservationRequest(30))])
        self.scheduler.assign(self.state, self.world,
            [replace(proposal('purchase', reservation=ReservationRequest(100)),
                     kind='purchase', eligible_roles=(2,))], self.nav)
        raw = payload(2)
        raw['teamOur']['goldNum'] = 30
        self.scheduler.maintain(self.state, WorldParser().parse(raw), {}, self.nav)
        self.assertEqual(len(self.state.assignments), 2)
        self.assertEqual(self.state.jobs[first.id].lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(sum(r.gold for r in self.state.reservations.values()), 30)

    def test_unexpected_gold_loss_preserves_higher_priority_job(self):
        first = self.assign([proposal(priority=Priority.CRITICAL_DEFENSE, reservation=ReservationRequest(100))])
        self.scheduler.assign(self.state, self.world,
            [replace(proposal('optional', reservation=ReservationRequest(30), priority=Priority.OPTIONAL),
                     eligible_roles=(2,))], self.nav)
        raw = payload(2)
        raw['teamOur']['goldNum'] = 100
        self.scheduler.maintain(self.state, WorldParser().parse(raw), {}, self.nav)
        self.assertEqual(self.state.assignments, {1: first.id})

    def test_remaining_duration_decreases_instead_of_reusing_original_estimate(self):
        rec = self.assign([proposal(target=Pos(1, 4), estimated_duration=3, deadline=3)])
        raw = payload(2)
        raw['teamOur']['roles'][0]['pos'] = dict(x=1, y=2)
        self.scheduler.maintain(self.state, WorldParser().parse(raw), {}, self.nav)
        self.assertEqual(self.state.jobs[rec.id].lifecycle, Lifecycle.ACTIVE)

    def test_interaction_binding_can_be_an_obstacle(self):
        class InteractionJob(DummyJob):
            def remaining_duration(self, world, role_id, binding, navigation):
                stand = Pos(binding.x, binding.y - 1)
                return navigation.cost(world, world.role(role_id).pos, stand) + 1
        raw = payload()
        raw['mapInfo']['zones'] = [dict(pos=dict(x=1, y=4), neutralType='copper')]
        self.world = WorldParser().parse(raw)
        self.scheduler.factories['dummy'] = InteractionJob
        rec = self.assign([proposal(target=Pos(1, 4), deadline=3)])
        self.assertEqual(rec.binding, Pos(1, 4))

    def test_rebind_is_atomic_and_limited_to_fallbacks(self):
        old = self.assign([proposal(target=Pos(1, 4), fallback_targets=(Pos(2, 4),))])
        self.scheduler.maintain(self.state, self.world, {old.id: Signal.NEEDS_REBIND}, self.nav)
        rec = self.state.jobs[old.id]
        self.assertEqual(rec.binding, Pos(2, 4))
        self.assertNotIn(('binding', Pos(1, 4)), self.state.reservations[old.id].keys)
        self.assertIn(('binding', Pos(2, 4)), self.state.reservations[old.id].keys)
        self.scheduler.assert_invariants(self.state)

    def test_failed_rebind_releases_everything(self):
        old = self.assign([proposal(target=Pos(1, 4))])
        self.scheduler.maintain(self.state, self.world, {old.id: Signal.NEEDS_REBIND}, self.nav)
        self.assertEqual(self.state.jobs[old.id].lifecycle, Lifecycle.FAILED)
        self.assertFalse(self.state.reservations)

    def test_dead_role_and_expired_deadline_fail(self):
        for reason in ('dead', 'late'):
            self.state = RuntimeState()
            rec = self.assign([proposal(deadline=1)])
            raw = payload(2)
            if reason == 'dead':
                raw['teamOur']['roles'][0]['health'] = 0
            self.scheduler.maintain(self.state, WorldParser().parse(raw), {}, self.nav)
            self.assertEqual(self.state.jobs[rec.id].lifecycle, Lifecycle.FAILED)

    def test_task_job_keeps_pioneer_locked(self):
        task = replace(proposal('task'), eligible_roles=(3,))
        self.scheduler.assign(self.state, self.world, [task], self.nav)
        job_id = self.state.assignments[3]
        shop = replace(proposal('shop', utility=100), eligible_roles=(3,))
        self.scheduler.assign(self.state, self.world, [shop], self.nav)
        self.assertEqual(self.state.assignments[3], job_id)

    def test_planner_cannot_directly_control_role(self):
        class ReadOnlyPlanner:
            def propose(inner, world, memory, plan, reservations):
                self.assertIsInstance(memory, tuple)
                with self.assertRaises(TypeError):
                    reservations['ghost'] = ReservationRequest()
                self.assertFalse(hasattr(plan, 'assignments'))
                self.assertFalse(hasattr(world, 'roleCommandMap'))
                return []
        self.assertEqual(Agent([ReadOnlyPlanner()], development=True).respond(self.raw)['roleCommandMap'], {})
        for path in (ROOT / 'src/coregeek_v2').glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            if path.name != 'execution.py':
                self.assertFalse(any(isinstance(n, ast.Constant) and n.value == 'roleCommandMap'
                                     for n in ast.walk(tree)), path.name)


class NavigationTests(unittest.TestCase):
    def test_eight_directions_diagonal_corner_and_cost_consistency(self):
        raw = payload()
        raw['mapInfo']['zones'] = [dict(pos=dict(x=2, y=1), neutralType='stone'),
                                   dict(pos=dict(x=1, y=2), neutralType='vendor')]
        world, nav = WorldParser().parse(raw), Navigation()
        path = nav.path(world, Pos(1, 1), Pos(4, 4))
        self.assertEqual(path[1], Pos(2, 2))
        self.assertEqual(nav.cost(world, Pos(1, 1), Pos(4, 4)), len(path) - 1)
        self.assertEqual(nav.itinerary_cost(world, [Pos(1, 1), Pos(4, 4), Pos(6, 4)]), 5)

    def records(self, raw):
        world, state, scheduler = WorldParser().parse(raw), RuntimeState(), Scheduler({'dummy': DummyJob})
        scheduler.assign(state, world, [proposal('a'), replace(proposal('b'), eligible_roles=(2,))], Navigation())
        return world, [state.jobs[state.assignments[r]] for r in (1, 2)]

    def test_workers_do_not_move_into_same_cell(self):
        world, records = self.records(payload())
        result = MovementCoordinator().resolve(world, [(r, MoveIntent(Pos(2, 1))) for r in records], Navigation())
        self.assertEqual(len(result), 1)

    def test_no_role_position_swap_collision(self):
        raw = payload()
        raw['teamOur']['roles'][1]['pos']['x'] = 2
        world, records = self.records(raw)
        result = MovementCoordinator().resolve(world,
            [(records[0], MoveIntent(Pos(2, 1))), (records[1], MoveIntent(Pos(1, 1)))], Navigation())
        self.assertEqual(result, [])

    def test_no_move_into_stationary_role_or_building(self):
        world, records = self.records(payload())
        self.assertFalse(MovementCoordinator().resolve(world,
            [(records[0], MoveIntent(Pos(3, 1)))], Navigation()))
        raw = payload()
        raw['teamOur']['roles'].append(role(9, 'station', 7, 7))
        world = WorldParser().parse(raw)
        self.assertIsNone(Navigation().path(world, Pos(1, 1), Pos(8, 6)))


class FeedbackAndRuntimeTests(unittest.TestCase):
    def test_concurrent_duplicate_requests_commit_once(self):
        agent = Agent([Proposer([proposal(target=Pos(1, 4))])], {'dummy': DummyJob}, development=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: agent.respond(payload()), range(12)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(next(iter(agent.sessions.values())).runtime.sequence, 1)

    def test_official_request_example_is_accepted(self):
        # The documentation example contains a trailing comma before roles ].
        # Normalize the fixture only; the HTTP server still requires valid JSON.
        sample = (ROOT.parents[1] / 'docs/request.txt').read_text(encoding='utf-8-sig')
        raw = json.loads(re.sub(r',(\s*[}\]])', r'\1', sample))
        agent = Agent(development=True)
        self.assertEqual(agent.respond(raw), {'roleCommandMap': {}, 'prompt': '', 'executeCmd': ''})

    def test_job_persists_across_rounds_then_completes(self):
        agent = Agent([Proposer([proposal(target=Pos(1, 4))])], {'dummy': DummyJob}, development=True)
        raw = payload()
        ids = []
        for turn in range(1, 5):
            raw['roundNo'] = turn
            response = agent.respond(raw)
            state = next(iter(agent.sessions.values())).runtime
            if state.assignments:
                ids.append(state.assignments[1])
            command = response['roleCommandMap'].get('1')
            if command:
                raw['teamOur']['roles'][0]['pos'] = command['targetPos'][0]
                raw['lastRoundRoleActionResults'] = {'1': True}
            if turn == 3:
                agent.planners = ()
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(state.jobs[ids[0]].lifecycle, Lifecycle.COMPLETED)
        self.assertFalse(state.assignments)

    def test_old_outcome_does_not_update_new_job_but_updates_memory(self):
        agent = Agent([Proposer([proposal(target=Pos(1, 4))])], {'dummy': DummyJob}, development=True)
        raw = payload()
        response = agent.respond(raw)
        session = next(iter(agent.sessions.values()))
        old = session.runtime.jobs[session.runtime.assignments[1]]
        agent.scheduler.assign(session.runtime, WorldParser().parse(raw),
            [proposal('night', priority=Priority.NIGHT_PREP, target=Pos(4, 4))], Navigation())
        new_id = session.runtime.assignments[1]
        raw['roundNo'] = 2
        raw['teamOur']['roles'][0]['pos'] = response['roleCommandMap']['1']['targetPos'][0]
        raw['lastRoundRoleActionResults'] = {'1': True}
        agent.planners = ()
        agent.respond(raw)
        session = next(iter(agent.sessions.values()))
        self.assertEqual(session.runtime.jobs[new_id].behavior.stage, 'GO')
        self.assertEqual(session.memory.outcomes[0].origin_job_id, old.id)
        self.assertEqual(session.memory.outcomes[0].kind, 'MOVE_SUCCESS')

    def test_legal_true_is_not_business_success_and_treasure_uses_code(self):
        first, second = payload(), payload(2)
        second['lastRoundRoleActionResults'] = {'3': True}
        action = PreviousActionRecord(1, 3, 'old', 'BUY', freeze({'action': 'buy', 'name': 'x'}))
        build = lambda a: OutcomeBuilder().build(WorldParser().parse(first), (a,), WorldParser().parse(second))[0]
        self.assertEqual(build(action).kind, 'UNCONFIRMED')
        action = replace(action, command=freeze({'action': 'summonTreasure'}))
        second['lastSummonTreasureResult'] = 3
        self.assertEqual(build(action).kind, 'TREASURE_WRONG_ITEMS')
        second['lastSummonTreasureResult'] = 1
        self.assertEqual(build(action).kind, 'TREASURE_SUCCESS')

    def test_skipped_round_feedback_is_unconfirmed(self):
        first, second = WorldParser().parse(payload()), WorldParser().parse(payload(3))
        action = PreviousActionRecord(1, 1, 'old', 'GO', freeze({'action': 'move', 'targetPos': [{'x': 1, 'y': 1}]}))
        self.assertEqual(OutcomeBuilder().build(first, (action,), second)[0].kind, 'UNCONFIRMED')

    def test_duplicate_round_is_idempotent_and_response_is_detached(self):
        agent = Agent([Proposer([proposal(target=Pos(1, 4))])], {'dummy': DummyJob}, development=True)
        first = agent.respond(payload())
        first['roleCommandMap'].clear()
        self.assertTrue(agent.respond(payload())['roleCommandMap'])
        session = next(iter(agent.sessions.values()))
        self.assertEqual(len(session.runtime.jobs), 1)
        self.assertEqual(session.memory.outcomes, [])

    def test_failed_request_does_not_commit_partial_state(self):
        agent = Agent(development=True)
        agent.respond(payload())
        before = next(iter(agent.sessions.values()))
        agent.planners = (Proposer([proposal()]),)
        with self.assertRaises(ValueError):
            agent.respond(payload(2))
        self.assertIs(next(iter(agent.sessions.values())), before)
        self.assertEqual(before.round_no, 1)

    def test_competition_exception_fallback_preserves_state(self):
        agent = Agent()
        agent.respond(payload())
        before = next(iter(agent.sessions.values()))
        with self.assertLogs('coregeek_v2.runtime', level='ERROR'):
            self.assertEqual(agent.respond({'roundNo': 2})['roleCommandMap'], {})
        self.assertIs(next(iter(agent.sessions.values())), before)

    def test_team_isolation_and_round_rewind_reset(self):
        agent = Agent(development=True)
        agent.respond(payload(5))
        other = payload(6)
        other['teamOur']['teamId'] = 'other'
        agent.respond(other)
        self.assertEqual(len(agent.sessions), 2)
        old = agent.sessions[('team', 'challenger')]
        agent.respond(payload())
        self.assertIsNot(agent.sessions[('team', 'challenger')], old)
        self.assertEqual(agent.sessions[('other', 'challenger')].round_no, 6)

    def test_day_70_night_ready_with_test_jobs(self):
        class NightProposer:
            def propose(self, world, memory, plan, reservations):
                return [proposal('night', priority=Priority.NIGHT_PREP, target=Pos(1, 4), deadline=70)]
        agent = Agent([Proposer([proposal()])], {'dummy': DummyJob}, development=True)
        raw = payload(67)
        agent.respond(raw)
        old = next(iter(agent.sessions.values())).runtime.assignments[1]
        agent.planners = (NightProposer(),)
        for turn in (68, 69, 70):
            raw['roundNo'] = turn
            command = agent.respond(raw)['roleCommandMap']['1']
            raw['teamOur']['roles'][0]['pos'] = command['targetPos'][0]
            raw['lastRoundRoleActionResults'] = {'1': True}
        self.assertEqual(raw['teamOur']['roles'][0]['pos'], {'x': 1, 'y': 4})
        self.assertEqual(next(iter(agent.sessions.values())).runtime.jobs[old].lifecycle, Lifecycle.PREEMPTED)
        raw['roundNo'] = 71
        agent.planners = ()
        agent.respond(raw)
        self.assertFalse(next(iter(agent.sessions.values())).runtime.assignments)
        for round_no, day, is_day, phase_round in ((70, 1, True, 70), (71, 1, False, 1),
                                                  (130, 1, False, 60), (131, 2, True, 1)):
            world = WorldParser().parse(payload(round_no))
            self.assertEqual((world.day_no, world.is_day, world.phase_round), (day, is_day, phase_round))


class ExecutionTests(unittest.TestCase):
    def test_command_cannot_spend_other_jobs_reserved_money(self):
        raw = payload()
        raw['mapInfo']['zones'] = [dict(pos=dict(x=1, y=2), neutralType='weaponShop')]
        raw['weaponShopList'] = [dict(name='WeaponUpgradeVoucher1', price=100)]
        world, state = WorldParser().parse(raw), RuntimeState()
        Scheduler({'dummy': DummyJob}).assign(state, world, [proposal()], Navigation())
        job_id = state.assignments[1]
        command = CommandEnvelope(1, job_id, 'BUY', {'action': 'buy', 'name': 'WeaponUpgradeVoucher1'})
        with self.assertRaisesRegex(AssertionError, 'reserved budget'):
            Validator().validate(world, [command], True, state)

    def test_missing_action_is_dropped_in_competition_mode(self):
        with self.assertLogs('coregeek_v2.execution', level='ERROR'):
            self.assertEqual(Validator().validate(WorldParser().parse(payload()),
                             [CommandEnvelope(1, 'a', 's', {})], False), [])

    def test_entrypoint_serves_http_in_separate_process(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'main3.py'), str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    request = Request(f'http://127.0.0.1:{port}', data=json.dumps(payload()).encode())
                    with urlopen(request, timeout=1) as response:
                        self.assertEqual(json.load(response)['roleCommandMap'], {})
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('HTTP entrypoint did not start')
                    time.sleep(0.05)
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_action_intent_cannot_bypass_movement_coordination(self):
        scheduler, state = Scheduler({'dummy': DummyJob}), RuntimeState()
        scheduler.assign(state, WorldParser().parse(payload()), [proposal()], Navigation())
        rec = state.jobs[state.assignments[1]]
        with self.assertRaisesRegex(TypeError, 'MoveIntent'):
            Executor().translate([(rec, ActionIntent('move', freeze({'targetPos': [{'x': 2, 'y': 2}]})))])

    def test_malformed_target_does_not_discard_valid_other_role(self):
        commands = [CommandEnvelope(1, 'a', 'GO', {'action': 'move', 'targetPos': [None]}),
                    CommandEnvelope(2, 'b', 'GO', {'action': 'move', 'targetPos': [{'x': 3, 'y': 2}]})]
        with self.assertLogs('coregeek_v2.execution', level='ERROR'):
            valid = Validator().validate(WorldParser().parse(payload()), commands, False)
        self.assertEqual([c.role_id for c in valid], [2])

    def test_one_role_one_command_per_round(self):
        commands = [CommandEnvelope(1, 'a', 'GO', {'action': 'move', 'targetPos': [{'x': 1, 'y': 2}]}),
                    CommandEnvelope(1, 'b', 'GO', {'action': 'move', 'targetPos': [{'x': 2, 'y': 2}]})]
        with self.assertRaisesRegex(AssertionError, 'duplicate'):
            Validator().validate(WorldParser().parse(payload()), commands, True)
        with self.assertLogs('coregeek_v2.execution', level='ERROR'):
            self.assertEqual(Validator().validate(WorldParser().parse(payload()), commands, False), [])

    def test_invalid_commands_are_dropped_individually(self):
        commands = [CommandEnvelope(1, 'a', 'GO', {'action': 'move', 'targetPos': [{'x': 1, 'y': 2}]}),
                    CommandEnvelope(3, 'b', 'GO', {'action': 'collect', 'targetPos': [{'x': 5, 'y': 2}]})]
        with self.assertLogs('coregeek_v2.execution', level='ERROR'):
            result = Validator().validate(WorldParser().parse(payload()), commands, False)
        self.assertEqual([c.role_id for c in result], [1])

    def test_missing_parameters_and_phase_rules(self):
        for cmd in ({'action': 'move'}, {'action': 'unknown'}, {'action': 'collect', 'targetPos': []},
                    {'action': 'attack', '_weapon_id': 9, 'targetPos': []}):
            with self.subTest(cmd=cmd), self.assertRaises(AssertionError):
                Validator().validate(WorldParser().parse(payload()), [CommandEnvelope(1, 'a', 's', cmd)], True)

    def test_stone_is_sellable(self):
        raw = payload()
        raw['teamOur']['roles'][0]['backpack'] = ['stone']
        raw['mapInfo']['zones'] = [dict(pos=dict(x=1, y=2), neutralType='vendor')]
        cmd = CommandEnvelope(1, 'a', 's', {'action': 'sell', 'name': 'stone', 'num': 1})
        self.assertEqual(Validator().validate(WorldParser().parse(raw), [cmd], True), [cmd])

    def test_attack_uses_weapon_key_and_controller_id(self):
        raw = payload(71)
        raw['teamOur']['roles'].append(role(9, 'railgun', 1, 2))
        scheduler, state = Scheduler({'dummy': DummyJob}), RuntimeState()
        world = WorldParser().parse(raw)
        scheduler.assign(state, world, [proposal()], Navigation())
        rec = state.jobs[state.assignments[1]]
        executor = Executor()
        commands = executor.translate([(rec, ActionIntent('attack', freeze({'_weapon_id': 9,
                                                                  'targetPos': [{'x': 1, 'y': 5}]})))])
        result = executor.command_map(Validator().validate(world, commands, True))
        self.assertEqual(result['9']['controllerId'], '1')
        self.assertNotIn('_weapon_id', result['9'])

    def test_http_protocol_and_import_isolation(self):
        server = make_server(0, host='127.0.0.1')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f'http://127.0.0.1:{server.server_port}',
                              data=json.dumps(payload()).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(json.load(response), {'roleCommandMap': {}, 'prompt': '', 'executeCmd': ''})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertNotIn('agent', sys.modules)
        import coregeek_v2
        self.assertTrue(Path(coregeek_v2.__file__).is_relative_to(ROOT))
        process = subprocess.run([sys.executable, '-B', str(ROOT / 'main3.py')], capture_output=True, text=True)
        self.assertIn('Usage:', process.stderr)


if __name__ == '__main__':
    unittest.main()
