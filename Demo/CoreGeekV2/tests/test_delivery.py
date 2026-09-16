"""Regression scenarios for the playable demo, using official payload fields."""
import unittest
from dataclasses import replace

from test_strategy_v2 import fixture, context, unit, armed
from coregeek_v2.jobs import BuildJob, ProcurementJob, FACTORIES
from coregeek_v2.model import (ActionIntent, MoveIntent, JobRecord, JobProposal,
                               GameMemory, GameMemoryReducer, Signal, ReservationRequest, RuntimeState, Lifecycle)
from coregeek_v2.scheduler import Scheduler
from coregeek_v2.runtime import Agent
from coregeek_v2.feedback import Outcome
from coregeek_v2.rules import command_error
from coregeek_v2.navigation import MovementCoordinator, Navigation
from coregeek_v2.strategy import ConstructionPlanner, DefensePlanner, layout
from coregeek_v2.world import WorldParser, Pos, freeze


class ConstructionRegressionTests(unittest.TestCase):
    def pocket_fixture(self):
        raw = fixture(131)
        raw['mapInfo'] = dict(width=41,height=32,zones=[])
        raw['teamOur']['roles'] = [unit(10,'station',30,7),unit(1,'worker',26,7),
                                    unit(2,'worker',29,10,backpack=['stone']),unit(3,'pioneer',29,8)]
        raw['teamEnemy']['roles'] = [unit(20,'station',10,24)]
        guns,walls,_ = layout(WorldParser().parse(raw))
        raw['teamOur']['roles'] += [unit(30+i,k,p.x,p.y) for i,(k,p) in enumerate(guns)]
        raw['teamOur']['roles'] += [unit(50+i,'wall',p.x,p.y) for i,p in enumerate(walls) if p != Pos(29,9)]
        return raw

    def test_last_wall_waits_for_ally_to_leave_enclosed_pocket(self):
        raw = self.pocket_fixture()
        c = context(raw)
        p = c.proposal('Build','last-wall',c.w.role(2),Pos(29,9),name='wall')
        job = BuildJob(p)
        self.assertIsNone(job.intent(c.w,2,p.target))
        raw['teamOur']['roles'][3]['pos'] = dict(x=28,y=10)
        intent = job.intent(WorldParser().parse(raw),2,p.target)
        self.assertEqual(intent.action,'build')

    def test_procurement_stand_avoids_future_enclosed_pocket(self):
        raw = self.pocket_fixture()
        c = context(raw)
        p = c.proposal('Delivery','upgrade',c.w.role(3),Pos(29,7),name='WeaponUpgradeVoucher1',target_id=31)
        job = ProcurementJob(p)
        stand = job.stand(c.w,c.w.role(3).pos,p.target,actual=True)
        self.assertNotEqual(stand,Pos(29,8))
        self.assertIsNotNone(stand)

    def test_new_proposals_exclude_occupied_sites(self):
        raw = fixture()
        c = context(raw)
        target = next(p for k, p in c.builds if k == 'rocket')
        raw['teamOur']['roles'][3]['pos'] = target.dump()
        c = context(raw)
        proposals = ConstructionPlanner(Navigation()).candidates(c)
        self.assertTrue(proposals)
        self.assertNotIn(target, [p.target for p in proposals])

    def test_active_builder_waits_until_ally_clears_site(self):
        raw = fixture()
        c = context(raw)
        p = next(p for p in ConstructionPlanner(Navigation()).candidates(c) if p.data['name'] == 'rocket')
        job = BuildJob(p)
        raw['teamOur']['roles'][3]['pos'] = p.target.dump()
        world = WorldParser().parse(raw)
        self.assertEqual(job.check(world, p.eligible_roles[0], p.target), Signal.CONTINUE)
        self.assertIsNone(job.intent(world, p.eligible_roles[0], p.target))
        raw['teamOur']['roles'][3]['pos'] = dict(x=0, y=0)
        self.assertIsNotNone(job.intent(WorldParser().parse(raw), p.eligible_roles[0], p.target))

    def test_construction_cell_is_excluded_from_movement_in_both_orders(self):
        raw = fixture()
        raw['teamOur']['roles'][1]['pos'] = dict(x=1, y=1)
        raw['teamOur']['roles'][2]['pos'] = dict(x=3, y=1)
        world = WorldParser().parse(raw)
        p = JobProposal('Build', 'build', (1,), 'test')
        r = JobRecord('builder', 1, p, BuildJob(p), Pos(2, 1))
        mover = replace(r, id='mover', role_id=2)
        build = (r, ActionIntent('build', freeze({'name':'wall', 'targetPos':[Pos(2, 1).dump()]})))
        move = (mover, MoveIntent(Pos(2, 1)))
        for intents in ([build, move], [move, build]):
            resolved = MovementCoordinator().resolve(world, intents, Navigation())
            self.assertEqual(resolved, [build])

    def test_movement_routes_around_same_turn_construction(self):
        raw = fixture()
        raw['teamOur']['roles'][1]['pos'] = dict(x=1, y=1)
        raw['teamOur']['roles'][2]['pos'] = dict(x=3, y=1)
        world = WorldParser().parse(raw)
        p = JobProposal('Build', 'build', (1,), 'test')
        r = JobRecord('builder', 1, p, BuildJob(p), Pos(2, 1))
        mover = replace(r, id='mover', role_id=2)
        resolved = MovementCoordinator().resolve(world, [
            (mover, MoveIntent(Pos(0, 1))),
            (r, ActionIntent('build', freeze({'name':'wall','targetPos':[Pos(2, 1).dump()]})))], Navigation())
        self.assertEqual(len(resolved), 2)
        self.assertNotEqual(resolved[0][1].goal, Pos(2, 1))


class NewsInputTests(unittest.TestCase):
    def test_wrong_array_types_do_not_abort_turn(self):
        world = WorldParser().parse(fixture())
        for value in (42, True, 'copper', {'kind':'copper'}, None):
            memory = GameMemory()
            GameMemoryReducer().accept_advice(memory, world, {'blocked_mines':value,'hold_ores':value})
            self.assertEqual(memory.advice, {'blocked_mines':[], 'hold_ores':[]})


class DeploymentRegressionTests(unittest.TestCase):
    def test_rejected_move_detours_and_critical_return_never_enters_cooldown(self):
        raw = fixture(60)
        raw['teamOur']['roles'][1]['pos'] = dict(x=1,y=1)
        c = context(raw)
        p = c.proposal('Return','return',c.w.role(1))
        job = FACTORIES['Return'](p)
        cmd = freeze({'command':{'action':'move','targetPos':[Pos(2,1).dump()]}})
        for turn in range(55,60):
            self.assertEqual(job.on_outcome(Outcome(turn,1,'return','RETURN','MOVE_FAILED',cmd)), Signal.RETRY)
        r = JobRecord('return',1,p,job,None)
        resolved = MovementCoordinator().resolve(c.w,[(r,MoveIntent(Pos(4,1)))],c.nav)
        self.assertEqual(len(resolved),1)
        self.assertNotEqual(resolved[0][1].goal,Pos(2,1))
        # The temporary obstacle expires; it is not a permanent map mutation.
        raw['roundNo'] = 65
        world = WorldParser().parse(raw)
        direct = MovementCoordinator().resolve(world,[(r,MoveIntent(Pos(2,1)))],Navigation())
        self.assertEqual(direct[0][1].goal,Pos(2,1))

    def test_return_budget_includes_future_walls_and_occupied_posts(self):
        raw = fixture()
        raw['teamOur']['roles'][1]['pos'] = dict(x=8,y=12)
        c = context(raw)
        worker = c.w.role(1)
        expected = c.nav.cost(c.w, worker.pos, c.post(worker), True, c.return_blocked(worker))
        self.assertGreater(expected, c.cost(worker.pos, c.post(worker)))
        self.assertEqual(c.tail(worker), expected)
        p = c.proposal('Return','return',worker)
        self.assertEqual(FACTORIES['Return'](p).return_cost(c.w,worker.pos), expected)

    def test_other_operators_do_not_seal_the_last_post(self):
        for mirror in (False, True):
            raw = fixture(131)
            if mirror:
                raw['teamOur']['roles'][0]['pos'] = dict(x=19, y=5)
                raw['teamEnemy']['roles'][0]['pos'] = dict(x=4, y=12)
            guns, walls, posts = layout(WorldParser().parse(raw))
            buildings = [raw['teamOur']['roles'][0]]
            buildings += [unit(30+i,k,p.x,p.y) for i,(k,p) in enumerate(guns)]
            buildings += [unit(50+i,'wall',p.x,p.y) for i,p in enumerate(walls)]
            for last, goal in posts.items():
                raw['teamOur']['roles'] = buildings + [
                    unit(100+i,'worker',p.x,p.y) for i,(name,p) in enumerate(posts.items()) if name != last]
                with self.subTest(mirror=mirror,last=last):
                    self.assertIsNotNone(Navigation().path(WorldParser().parse(raw),Pos(0,0),goal))

    def test_destroyed_station_cancels_active_jobs_and_keeps_protocol_alive(self):
        raw = armed(71)
        agent = Agent.production(development=True)
        agent.respond(raw)
        raw['roundNo'] = 72
        raw['teamOur']['roles'][0]['health'] = 0
        raw['robot']['roles'] = [dict(unit(80,'largeRobot',10,12,health=500),targetTeam='challenger')]
        result = agent.respond(raw)
        self.assertEqual(result, {'roleCommandMap':{},'prompt':'','executeCmd':''})
        state = next(iter(agent.sessions.values())).runtime
        self.assertFalse(state.assignments or state.reservations or state.previous_actions)
        self.assertTrue(all(r.lifecycle == Lifecycle.CANCELLED for r in state.jobs.values()))
        self.assertEqual(agent.respond(raw), result)


class ProcurementRegressionTests(unittest.TestCase):
    def test_station_voucher_uses_any_adjacent_footprint_tile(self):
        raw = fixture(261)
        raw['teamOur']['roles'][0]['health'] = 600
        raw['teamOur']['roles'][3]['pos'] = dict(x=4,y=10)
        raw['teamOur']['roles'][3]['backpack'] = ['StationUpgradeVoucher1']
        c = context(raw)
        p = c.proposal('Delivery','base',c.w.role(3),c.w.station().pos,
                       name='StationUpgradeVoucher1',target_id=10)
        intent = ProcurementJob(p).intent(c.w,3,p.target)
        self.assertEqual(intent.action,'use')
        self.assertIsNone(command_error(c.w,3,dict(intent.params,action=intent.action)))

    def test_held_delivery_precedes_a_new_purchase(self):
        raw = armed()
        raw['teamOur']['goldNum'] = 500
        raw['teamOur']['roles'][4]['level'] = 2
        raw['teamOur']['roles'][3]['backpack'] = ['WeaponUpgradeVoucher2']
        c = context(raw)
        proposals = [p for p in DefensePlanner(c.nav).candidates(c) if p]
        self.assertEqual(max(proposals, key=lambda p:p.utility).kind, 'Delivery')

    def test_engineer_quota_cannot_use_other_workers_inventory(self):
        raw = armed(131)
        raw['teamOur']['roles'][1]['backpack'] = ['stone'] * 20
        c = context(raw)
        self.assertEqual(c.stone, 0)
        self.assertEqual(c.stone_need, 11)
        self.assertEqual(c.keep_stone(c.w.role(1)), 0)
        self.assertEqual(c.keep_stone(c.w.role(2)), 11)

    def test_ordinary_wall_rebuild_is_in_stone_quota_and_avoids_voucher(self):
        raw = armed(131)
        raw['teamOur']['goldNum'] = 1000
        raw['teamOur']['roles'].append(unit(50,'wall',6,9,health=200))
        raw['teamOur']['roles'][2]['backpack'] = ['stone']
        c = context(raw)
        self.assertEqual([r.id for r in c.rebuilds], [50])
        self.assertEqual(c.stone_quota, 11)  # nine missing + one rebuild + spare
        proposals = [p for p in DefensePlanner(c.nav).candidates(c) if p]
        self.assertTrue(any(p.kind == 'Rebuild' and p.target == Pos(6,9) for p in proposals))
        self.assertFalse(any(p.kind == 'Procure' and p.target == Pos(6,9) for p in proposals))

    def test_price_increase_reconciles_reservation_before_purchase(self):
        raw = armed()
        raw['teamOur']['goldNum'] = 300
        c = context(raw)
        p = c.proposal('Procure', 'upgrade', c.w.role(3), c.w.role(30).pos,
                       gold=100, name='WeaponUpgradeVoucher1', target_id=30)
        scheduler, state = Scheduler(FACTORIES), RuntimeState()
        scheduler.assign(state, c.w, [p], c.nav)
        job_id = state.assignments[3]
        raw['roundNo'] = 2
        raw['weaponShopList'][0]['price'] = 125
        scheduler.maintain(state, WorldParser().parse(raw), {}, c.nav)
        self.assertEqual(state.reservations[job_id].gold, 125)
        # Inventory is authoritative even if a purchase result was missing.
        raw['teamOur']['roles'][3]['backpack'] = ['WeaponUpgradeVoucher1']
        scheduler.maintain(state, WorldParser().parse(raw), {}, c.nav)
        self.assertEqual(state.reservations[job_id].gold, 0)

    def test_unaffordable_changed_price_releases_job_and_reservation(self):
        raw = armed()
        raw['teamOur']['goldNum'] = 100
        c = context(raw)
        p = c.proposal('Procure', 'upgrade', c.w.role(3), c.w.role(30).pos,
                       gold=100, name='WeaponUpgradeVoucher1', target_id=30)
        scheduler, state = Scheduler(FACTORIES), RuntimeState()
        scheduler.assign(state, c.w, [p], c.nav)
        job_id = state.assignments[3]
        raw['roundNo'] = 2
        raw['weaponShopList'][0]['price'] = 125
        scheduler.maintain(state, WorldParser().parse(raw), {}, c.nav)
        self.assertNotIn(3, state.assignments)
        self.assertNotIn(job_id, state.reservations)
        self.assertEqual(state.jobs[job_id].lifecycle, Lifecycle.FAILED)

    def test_existing_three_weapons_do_not_trigger_fourth_build(self):
        raw = armed()
        raw['teamOur']['roles'][4]['roleType'] = 'gatling'
        c = context(raw)
        self.assertFalse(any(p and p.data['name'] != 'wall'
                             for p in ConstructionPlanner(c.nav).candidates(c)))


if __name__ == '__main__':
    unittest.main()
