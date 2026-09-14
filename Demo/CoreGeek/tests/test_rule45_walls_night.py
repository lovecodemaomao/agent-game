"""需求 4、5 的回归测试。

4. 夜战结束后: 工人立刻到下一天要去的矿点旁, 开拓者先到任务点旁
5. 石头不卖; 第3天围墙两端各加一格(10 -> 12); 第3天掉血的一级墙拆掉重建,
   掉血的二级墙用围墙修复包
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response, ring, wall_sites
from agent.memory import Memory
from agent.protocol import Pos, Turn, distance
from agent.economy import HP, WALL_MAINTENANCE_DAY

DAY = 130


def shops(shops_list=None):
    return [{'name': n, 'price': v} for n, v in (shops_list or [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30),
        ('StationUpgradeVoucher1', 100), ('StationUpgradeVoucher2', 150),
        ('WallFixer', 10)])]


def build_payload(day=3, gold=200, zones=None, walls=(), roles=None, robots=(), rod=1,
                  station_health=1500, tasks=False):
    p = fixture()
    p['roundNo'] = (day - 1) * DAY + rod
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = roles if roles is not None else [
        unit(1, 'station', 10, 24, health=station_health),
        unit(2, 'worker', 9, 24), unit(3, 'worker', 12, 24),
        unit(4, 'pioneer', 10, 25),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ] + list(walls)
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = shops()
    p['robot'] = {'roles': list(robots)}
    if tasks:
        p['mapInfo']['zones'] = list(p['mapInfo']['zones']) + [
            {'pos': {'x': 20, 'y': 20}, 'neutralType': 'challengerTaskPoint1'}]
        p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 20, 'y': 20}, 'isValid': True,
                                        'coldDownRounds': 0, 'timeoutRounds': 80,
                                        'scoreReward': 50, 'goldReward': 30,
                                        'taskType': '自进化类1'}]
    return p


def robot(uid, x, y, health=100):
    return {'id': uid, 'pos': {'x': x, 'y': y}, 'health': health,
            'roleType': 'smallRobot', 'targetTeam': 'challenger'}


class StoneIsBuildingMaterialTests(unittest.TestCase):
    """需求5: 石头不卖, 只用于建墙/修墙。"""

    def test_stone_is_never_a_sale_candidate(self):
        p = build_payload(day=2)
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 12
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.act(worker, planner.route(worker))
        command = planner.commands.get('2', {})
        self.assertNotEqual(command.get('action'), 'sell', command)

    def test_only_ore_is_sold_even_when_stone_is_in_the_pack(self):
        # 站在小贩旁且背包里同时有石头与铜矿 -> 只卖铜矿
        p = build_payload(day=2, zones=[{'pos': {'x': 8, 'y': 24}, 'neutralType': 'vendor'}])
        p['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 25}
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 8 + ['copper'] * 4
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.act(worker, planner.route(worker))
        command = planner.commands['2']
        self.assertEqual(command['action'], 'sell')
        self.assertEqual(command['name'], 'copper')
        self.assertNotIn('stone', command['name'])


class WallExtensionTests(unittest.TestCase):
    """需求5: 第3天白天把墙两边往后再多加一格, 变成 12 格。"""

    def test_forward_half_is_ten_cells_by_default(self):
        self.assertEqual(len(wall_sites(Turn.load(build_payload(day=2)))), 10)

    def test_day3_extends_to_twelve_cells(self):
        turn = Turn.load(build_payload(day=3))
        base = wall_sites(turn)
        extended = wall_sites(turn, extend=1)
        self.assertEqual(len(extended), 12)
        extra = [p for p in extended if p not in base]
        self.assertEqual(len(extra), 2)
        for pos in extra:
            self.assertIn(pos, ring(turn, 2), '新增格必须仍在围墙环上')
            self.assertTrue(any(distance(pos, q) <= 2 for q in base), '新增格应接在墙的两端')

    def test_planner_uses_twelve_walls_from_day3(self):
        for day, expected in ((2, 10), (3, 12), (4, 12)):
            p = build_payload(day=day)
            planner = Planner(Turn.load(p), p, Memory(day=day))
            self.assertEqual(len(planner.walls), expected, f'day {day}')

    def test_extension_cells_are_built_by_workers(self):
        # 第3天缺的墙(含两端的加长格)要被列入待建清单
        p = build_payload(day=3)
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertEqual(len(planner.missing_walls()), 12)


class WallMaintenanceTests(unittest.TestCase):
    """需求5: 第3天掉血的一级墙拆掉重建, 掉血的二级墙用修复包。"""

    def level1_damaged_payload(self, day=3, health=900, rod=10):
        sites = wall_sites(Turn.load(build_payload(day=day)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=health)
        p = build_payload(day=day, walls=[wall], rod=rod)
        p['teamOur']['roles'][1]['pos'] = {'x': sites[0].x - 1, 'y': sites[0].y}
        return p, sites[0]

    def test_level1_damaged_wall_is_a_rebuild_target_on_day3(self):
        p, _ = self.level1_damaged_payload()
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertEqual([w.unit_id for w in planner.economic.rebuild_targets()], [40])

    def test_level1_damaged_wall_is_not_rebuilt_before_day3(self):
        p, _ = self.level1_damaged_payload(day=2)
        planner = Planner(Turn.load(p), p, Memory(day=2))
        self.assertEqual(planner.economic.rebuild_targets(), [])

    def test_level1_damaged_wall_takes_no_upgrade_voucher_on_day3(self):
        p, _ = self.level1_damaged_payload()
        planner = Planner(Turn.load(p), p, Memory(day=3))
        items = [o[3] for o in planner.economic.options()]
        self.assertNotIn('WallUpgradeVoucher1', items)
        self.assertNotIn('WallFixer', items)

    def test_worker_removes_then_rebuilds_the_wall(self):
        p, pos = self.level1_damaged_payload()
        p['teamOur']['roles'][1]['backpack'] = ['stone']
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertTrue(planner.maintain_walls(worker, planner.route(worker)))
        self.assertEqual(planner.commands['2'], {'action': 'remove', 'targetPos': [pos.dump()]})
        # 下一回合: 墙已拆掉 -> 原地重建(消耗一块石头)
        p['roundNo'] += 1
        del p['teamOur']['roles'][[r['id'] for r in p['teamOur']['roles']].index(40)]
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertTrue(planner.maintain_walls(worker, planner.route(worker)))
        command = planner.commands['2']
        self.assertEqual(command['action'], 'build')
        self.assertEqual(command['name'], 'wall')
        self.assertEqual(Pos.load(command['targetPos'][0]), pos)

    def test_rebuild_needs_a_stone_so_worker_fetches_first(self):
        p, pos = self.level1_damaged_payload()
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        job = planner.memory.wall_rebuilds.setdefault(2, {'pos': pos, 'stage': 'build', 'unit': 40})
        del p['teamOur']['roles'][[r['id'] for r in p['teamOur']['roles']].index(40)]
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertTrue(planner.maintain_walls(worker, planner.route(worker)))
        command = planner.commands['2']
        self.assertIn(command['action'], ('move', 'collect'), command)

    def test_only_one_worker_is_assigned_to_rebuilding(self):
        p, pos = self.level1_damaged_payload()
        m = Memory(day=3)
        m.wall_rebuilds[3] = {'pos': Pos(pos.x, pos.y + 1), 'stage': 'build', 'unit': 41}
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.maintain_walls(worker, planner.route(worker)))

    def test_level2_damaged_wall_is_repaired_with_fixer_on_day3(self):
        sites = wall_sites(Turn.load(build_payload(day=3)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=1400, level=2)
        p = build_payload(day=3, walls=[wall])
        p['teamOur']['roles'][1]['pos'] = {'x': sites[0].x - 1, 'y': sites[0].y}
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer']
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertIn(40, [w.unit_id for w in planner.economic.damaged_walls()])
        planner.run()
        command = planner.commands.get('2')
        self.assertIsNotNone(command, planner.commands)
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'WallFixer')

    def test_slight_damage_is_ignored_before_day3(self):
        sites = wall_sites(Turn.load(build_payload(day=2)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=1200, level=2)   # 80% > 前排阈值70%
        p = build_payload(day=2, walls=[wall])
        planner = Planner(Turn.load(p), p, Memory(day=2))
        self.assertEqual(planner.economic.damaged_walls(), [])

    def test_no_wall_voucher_for_wall_that_will_be_rebuilt(self):
        p, _ = self.level1_damaged_payload()
        planner = Planner(Turn.load(p), p, Memory(day=3))
        worker = planner.turn.workers()[0]
        count = planner.economic.purchase_count(worker, 'WallUpgradeVoucher1', 20, 4)
        self.assertEqual(count, 0, '掉血的一级墙要重建, 不该为它买升级券')


class NightPrepositionTests(unittest.TestCase):
    """需求4: 夜战结束后工人去次日矿点旁, 开拓者去任务点旁。"""

    def night_payload(self, robots=(), tasks=False, rod=100, day=1, gold=0, roles=None):
        p = build_payload(day=day, gold=gold, robots=robots, tasks=tasks, rod=rod, roles=roles)
        return p

    def test_worker_moves_toward_next_day_mine_when_battle_is_over(self):
        p = self.night_payload(rod=100)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        stand = planner.economic.next_day_mine(worker, planner.route(worker))
        self.assertIsNotNone(stand)
        mines = [pos for pos, kind in planner.turn.zones.items() if kind in ('stone', 'iron', 'copper')]
        self.assertEqual(min(distance(stand, mine) for mine in mines), 1,
                         '预置站位必须是矿点的采集邻格')

    def test_worker_stays_when_robots_are_still_attacking(self):
        p = self.night_payload(rod=100, robots=[robot(1, 9, 24)])
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        self.assertFalse(planner.night_battle_over())

    def test_worker_stays_before_the_last_night_rounds(self):
        # 场上仍有机器人(虽离炮位远)且未到夜末 -> 不提前离开炮位
        p = self.night_payload(rod=100, robots=[robot(9, 38, 2)])
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        self.assertFalse(planner.night_battle_over())
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.preposition(worker), '夜里前段仍应守炮位')

    def test_worker_leaves_in_the_last_night_rounds(self):
        p = self.night_payload(rod=126)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.prepare()
        self.assertTrue(planner.preposition(worker))
        self.assertEqual(planner.commands['2']['action'], 'move')
        self.assertIn(2, m.prepositioned)

    def test_no_preposition_into_a_cell_next_to_a_robot(self):
        p = self.night_payload(rod=126, robots=[robot(1, 6, 22)])
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.preposition(worker))

    def test_prepositioned_role_is_not_pulled_back_to_a_tower(self):
        p = self.night_payload(rod=126)
        m = Memory(day=1)
        m.prepositioned.add(2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        planner.assign_towers()
        self.assertNotIn('2', planner.commands, planner.commands)

    def test_preposition_resets_on_the_next_day(self):
        p = self.night_payload(rod=126)
        m = Memory(day=1)
        m.prepositioned.add(2)
        p['roundNo'] = DAY + 1                 # 新的一天
        decide_response(p, m)
        self.assertEqual(m.prepositioned, set())

    def test_pioneer_goes_to_the_task_point_when_idle(self):
        p = self.night_payload(rod=126, tasks=True)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        pioneer = [r for r in planner.turn.alive(('pioneer',))][0]
        stand = __import__('agent.tasks', fromlist=['Tasks']).Tasks(planner).next_day_post(pioneer)
        self.assertIsNotNone(stand)
        point = Pos(20, 20)
        self.assertEqual(distance(stand, point), 1, '开拓者应站在任务点旁')

    def test_pioneer_does_not_move_while_a_task_is_active(self):
        p = self.night_payload(rod=126, tasks=True)
        m = Memory(day=1)
        m.task = {'desc': '任务', 'start': 1, 'timeout': 100, 'token': 'x', 'positions': [Pos(20, 20)],
                  'history': [], 'proposal': None, 'waiting_cmd': False, 'skill': '', 'probes': 0,
                  'probe_results': [], 'probing': False, 'result': '', 'result_seq': 0,
                  'derived_seq': 0, 'tpl_tries': 0, 'tpl_last': ''}
        planner = Planner(Turn.load(p), p, m)
        pioneer = [r for r in planner.turn.alive(('pioneer',))][0]
        self.assertIsNone(__import__('agent.tasks', fromlist=['Tasks']).Tasks(planner).next_day_post(pioneer))

    def test_everyone_walks_to_the_post_over_following_rounds(self):
        # 连续夜末回合: 工人最终应停在矿点旁(而不是被炮位分配反复拉回)
        p = self.night_payload(rod=124)
        m = Memory(day=1)
        for _ in range(8):
            planner = Planner(Turn.load(p), p, m)
            planner.run()
            command = planner.commands.get('2')
            if command and command['action'] == 'move':
                for role in p['teamOur']['roles']:
                    if role['id'] == 2:
                        role['pos'] = command['targetPos'][0]
            p['roundNo'] += 1
            if p['roundNo'] % DAY == 1:
                break
        worker = next(r for r in p['teamOur']['roles'] if r['id'] == 2)
        mines = [Pos.load(z['pos']) for z in p['mapInfo']['zones']
                 if z['neutralType'] in ('stone', 'iron', 'copper')]
        self.assertEqual(min(distance(Pos.load(worker['pos']), mine) for mine in mines), 1,
                         worker['pos'])


if __name__ == '__main__':
    unittest.main()
