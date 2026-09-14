"""Regression scenarios for inventory delivery and multi-round movement recovery."""
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parent)]
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response
from agent.memory import Memory
from agent.protocol import Pos, Turn, move_command
from agent.grid import neighbours


def scenario():
    p = fixture()
    p['roundNo'] = 85
    p['teamOur']['goldNum'] = 0
    p['teamOur']['roles'] = [unit(1, 'station', 10, 24),
                              unit(2, 'worker', 8, 24),
                              unit(10, 'rocket', 9, 23, health=1000)]
    return p


class InventoryRecoveryTests(unittest.TestCase):
    def test_pioneer_delivers_and_consumes_voucher_across_rounds(self):
        p = scenario()
        role = p['teamOur']['roles'][1]
        role.update(roleType='pioneer', pos={'x': 5, 'y': 24},
                    backpack=['WeaponUpgradeVoucher1'])
        m = Memory()
        used = 0
        for _ in range(12):
            result = decide_response(p, m)
            cmd = result['roleCommandMap'].get('2', {})
            if cmd.get('action') == 'move':
                self.assertNotIn(Pos.load(cmd['targetPos'][0]), Turn.load(p).occupied_cells())
                role['pos'] = cmd['targetPos'][0]
            elif cmd.get('action') == 'use':
                role['backpack'].remove(cmd['name'])
                p['teamOur']['roles'][2]['level'] = 2
                used += 1
            p['roundNo'] += 1
            p['lastRoundRoleActionResults'] = {'2': True}
        self.assertEqual(used, 1)
        self.assertEqual(role['backpack'], [])
        self.assertEqual(m.day_weapon_upgrades, 1)
        self.assertNotIn(2, m.jobs)

    def test_wall_voucher_not_hidden_by_another_roles_fixer(self):
        p = scenario()
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        p['teamOur']['roles'] += [unit(3, 'worker', 7, 24, backpack=['WallFixer']),
                                  unit(40, 'wall', 13, 23, health=400)]
        a = Planner(Turn.load(p), p, Memory(day=1))
        a.economic.prepare()
        self.assertEqual(a.memory.jobs[2]['item'], 'WallUpgradeVoucher1')
        self.assertNotIn(3, a.memory.jobs)  # upgrade already restores this wall

    def test_unreachable_building_does_not_steal_voucher(self):
        p = scenario()
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        target = Pos(5, 10)
        p['teamOur']['roles'].append(unit(11, 'rocket', target.x, target.y))
        p['teamOur']['roles'] += [unit(100+i, 'wall', q.x, q.y, health=1000)
                                  for i, q in enumerate(neighbours(target))]
        m = Memory(day=1, jobs={2: {'type': 'upgrade', 'unit': 11,
                    'item': 'WeaponUpgradeVoucher1', 'level': 1, 'bought': True}})
        a = Planner(Turn.load(p), p, m)
        a.economic.prepare()
        self.assertEqual(m.jobs[2]['unit'], 10)
        self.assertIn((2, 11), m.blocked_deliveries)

    def test_orphan_summon_order_is_used(self):
        p = scenario()
        p['teamOur']['roles'][1]['backpack'] = ['LargeRobotSummonOrder']
        result = decide_response(p, Memory())
        self.assertEqual(result['roleCommandMap']['2'],
                         {'action': 'use', 'name': 'LargeRobotSummonOrder'})

    def test_medicine_used_during_day_away_from_tower(self):
        p = scenario(); p['roundNo'] = 1
        p['teamOur']['roles'][1].update(health=40, backpack=['Medicine'])
        result = decide_response(p, Memory())
        self.assertEqual(result['roleCommandMap']['2'], {'action': 'use', 'name': 'Medicine'})

    def test_full_level_target_leaves_obsolete_voucher_in_inventory(self):
        p = scenario(); p['teamOur']['roles'][2]['level'] = 3
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        result = decide_response(p, Memory())
        self.assertFalse(any(c['action'] == 'use' for c in result['roleCommandMap'].values()))

    def test_active_task_is_not_overwritten_by_inventory_use(self):
        p = scenario(); p['roundNo'] = 10
        p['teamOur']['roles'][1].update(roleType='pioneer', backpack=['WeaponUpgradeVoucher1'])
        p['phaseTask'] = 'Read the task instructions and submit the answer'
        p['mapInfo']['zones'] = [{'pos': {'x': 7, 'y': 24}, 'neutralType': 'challengerTaskPoint1'}]
        p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 7, 'y': 24},
                                      'isValid': True, 'timeoutRounds': 100}]
        result = decide_response(p, Memory())
        self.assertTrue(result['executeCmd'])
        self.assertNotIn('2', result['roleCommandMap'])

    def test_failed_use_does_not_count_as_upgrade(self):
        p = scenario()
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        m = Memory()
        for _ in range(3):
            decide_response(p, m)
            p['roundNo'] += 1
            p['lastRoundRoleActionResults'] = {'2': False}
        self.assertEqual(m.day_weapon_upgrades, 0)

    def test_batch_limited_by_eligible_targets_and_capacity(self):
        p = scenario(); p['teamOur']['goldNum'] = 1000
        role = p['teamOur']['roles'][1]
        role.update(backPackCapability=2, backpack=['stone'])
        a = Planner(Turn.load(p), p, Memory(day=1))
        self.assertEqual(a.economic.purchase_count(a.turn.workers()[0],
                         'WeaponUpgradeVoucher1', 100, 10), 1)
        p['teamOur']['roles'][2]['level'] = 2
        a = Planner(Turn.load(p), p, Memory(day=1))
        self.assertEqual(a.economic.purchase_count(a.turn.workers()[0],
                         'WeaponUpgradeVoucher1', 100, 10), 0)

    def test_purchase_rechecks_total_cost_after_other_spending(self):
        p = scenario(); p['roundNo'] = 10; p['teamOur']['goldNum'] = 100
        p['mapInfo']['zones'] = [{'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'}]
        m = Memory(day=1, jobs={2: {'type': 'upgrade', 'unit': 10,
                    'item': 'WeaponUpgradeVoucher1', 'level': 1, 'bought': False,
                    'price': 100, 'num': 2, 'shop': Pos(7, 24)}})
        a = Planner(Turn.load(p), p, m)
        self.assertTrue(a.economic.upgrade(a.turn.workers()[0], a.route(a.turn.workers()[0])))
        self.assertEqual(a.commands['2']['num'], 1)
        self.assertEqual(a.gold, 0)


class MovementRecoveryTests(unittest.TestCase):
    def test_repeated_blocked_moves_defer_delivery(self):
        p = scenario(); m = Memory()
        m.jobs[2] = {'type': 'upgrade', 'unit': 10, 'item': 'WeaponUpgradeVoucher1', 'level': 1}
        goal = Pos(8, 22)
        for _ in range(4):
            turn = Turn.load(p); m.observe(turn, p)
            m.movement[2] = goal
            m.remember(turn, {'roleCommandMap': {'2': move_command(Pos(8, 23))}, 'prompt': '', 'executeCmd': ''})
            p['roundNo'] += 1
        self.assertIn((2, goal), m.blocked_goals)
        self.assertIn((2, 10), m.blocked_deliveries)
        self.assertNotIn(2, m.jobs)

    def test_two_cell_cycle_is_detected(self):
        p = scenario(); m = Memory(); goal = Pos(8, 20)
        for x in (8, 7, 8, 7, 8):
            p['teamOur']['roles'][1]['pos'] = {'x': x, 'y': 24}
            turn = Turn.load(p); m.observe(turn, p)
            m.movement[2] = goal
            m.remember(turn, {'roleCommandMap': {'2': move_command(Pos(15-x, 24))}})
            p['roundNo'] += 1
        self.assertIn((2, goal), m.blocked_goals)

    def test_stale_route_cannot_enter_newly_reserved_cell(self):
        p = scenario(); a = Planner(Turn.load(p), p, Memory(day=1))
        role = a.turn.workers()[0]; routes = a.route(role); goal = Pos(7, 24)
        a.reserved.add(goal)
        self.assertFalse(a.move(role, routes, goal))
        self.assertNotIn('2', a.commands)

    def test_inner_detour_compared_with_nearest_interaction(self):
        p = scenario(); a = Planner(Turn.load(p), p, Memory(day=1))
        class Detour:
            cost = {Pos(12, 23): 20}
            def distance(self, target): return 0
        self.assertIsNone(a.inner_stand(Detour(), Pos(13, 23), 'use'))

    def test_existing_tower_post_survives_small_cost_change(self):
        p = scenario()
        p['teamOur']['roles'] += [unit(11, 'rocket', 9, 25)]
        m = Memory(day=1, tower_assignments={2: 11})
        a = Planner(Turn.load(p), p, m); a.assign_towers()
        self.assertEqual(m.tower_assignments[2], 11)

    def test_arrived_role_uses_item_instead_of_returning_to_old_stand(self):
        p = scenario(); p['roundNo'] = 10
        m = Memory(day=1, movement={2: Pos(8, 22)})
        a = Planner(Turn.load(p), p, m); role = a.turn.workers()[0]
        self.assertTrue(a.interact(role, a.route(role), Pos(9, 23), 'use',
                                   name='WeaponUpgradeVoucher1', targetPos=[Pos(9, 23).dump()]))
        self.assertEqual(a.commands['2']['action'], 'use')

    def test_wall_cannot_close_last_exit(self):
        p = scenario(); trapped = Pos(8, 24); gap = Pos(7, 24)
        p['teamOur']['roles'] += [unit(100+i, 'wall', q.x, q.y, health=1000)
            for i, q in enumerate(neighbours(trapped)) if q != gap and q != Pos(9, 23)]
        a = Planner(Turn.load(p), p, Memory(day=1))
        self.assertFalse(a.construction_accessible(gap))


if __name__ == '__main__':
    unittest.main()
