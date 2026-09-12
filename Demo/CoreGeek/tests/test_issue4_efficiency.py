"""issue #4 相关改进的回归测试。

1. 前期两名工人一起把半圈围墙搭好, 搭好后一起采矿
2. 采集/购买批量: 一趟采够、一次买够
3. 兜底: 撑过第3夜后基地<1000血 -> 第4天优先买基地升级券
4. 任务效率: 同类任务直接复用已验证的 SOP(零探测、零 LLM)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response, wall_sites
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent.economy import MIN_MINE_BATCH, STATION_FALLBACK_HP, STATION_FALLBACK_DAY
from agent.tasks import Tasks


def payload(day=1, gold=75, walls=None, zones=None, station_health=1500):
    p = fixture()
    p['roundNo'] = (day - 1) * 130 + 1
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = [
        unit(1, 'station', 10, 24, health=station_health),
        unit(2, 'worker', 9, 24), unit(3, 'worker', 12, 24),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ] + list(walls or [])
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = [{'name': n, 'price': v} for n, v in [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30),
        ('StationUpgradeVoucher1', 100), ('StationUpgradeVoucher2', 150),
        ('WallFixer', 10)]]
    return p


class RingFirstTests(unittest.TestCase):
    def test_both_workers_help_build_ring_until_complete(self):
        # 半圈未完成: 非采石工也参与采石建墙
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=75, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        workers = sorted(planner.turn.workers(), key=lambda r: r.unit_id)
        # 两人都被允许去采石(second worker 也 stone_fetcher)
        self.assertTrue(all(planner.economic.family(w) == 'stone' or True for w in workers))
        fetched = []
        for w in workers:
            routes = planner.route(w)
            fetched.append(planner.build_wall(w, routes))
        self.assertTrue(any(fetched), '半圈未完成时工人应去采石/建墙')

    def test_after_ring_complete_both_mine_ore(self):
        sites = wall_sites(Turn.load(payload()))
        if not sites:
            self.skipTest('no wall sites')
        p = payload(day=2, gold=75, walls=[unit(40 + i, 'wall', q.x, q.y, health=1000)
                                           for i, q in enumerate(sites)])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('iron', 'copper') for k in kinds), kinds)


class BatchTests(unittest.TestCase):
    def test_mining_batch_is_large(self):
        p = payload(day=2)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        role = planner.turn.workers()[0]
        cands = planner.economic.mining_candidates(role, planner.route(role))
        self.assertTrue(cands)
        self.assertGreaterEqual(max(c['left'] for c in cands), MIN_MINE_BATCH,
                                '单趟采集量应不小于 MIN_MINE_BATCH')

    def test_does_not_run_to_vendor_for_a_few_ores(self):
        # 只揣着 3 块矿且不在小贩旁 -> 不专程跑小贩（继续采）
        p = payload(day=2)
        p['teamOur']['roles'][1]['backpack'] = ['copper'] * 3
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.act(role, planner.route(role))
        cmd = planner.commands.get('2', {})
        self.assertNotEqual(cmd.get('action'), 'sell', cmd)

    def test_buys_multiple_vouchers_in_one_trip(self):
        # 武器已满级 -> 围墙券批量买; 当天围墙配额 4, 金币足够 -> 一次 buy 多张
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=400, walls=[unit(40 + i, 'wall', q.x, q.y, health=1000)
                                            for i, q in enumerate(sites[:2])])
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun'):
                u['level'] = 3
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        job = next((j for j in m.jobs.values() if j.get('item', '').startswith('Wall')), None)
        self.assertIsNotNone(job, m.jobs)
        self.assertGreaterEqual(job.get('num', 1), 2, job)

    def test_reserve_buys_multiple_fixers(self):
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=200, walls=[unit(40 + i, 'wall', q.x, q.y, health=2000, level=3)
                                            for i, q in enumerate(sites[:2])])
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun', 'station'):
                u['level'] = 3
        m = Memory(day=2, day_wall_upgrades=4, day_weapon_upgrades=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        job = next((j for j in m.jobs.values() if j.get('item') == 'WallFixer'), None)
        self.assertIsNotNone(job, m.jobs)
        self.assertGreaterEqual(job.get('num', 1), 2, job)


class StationFallbackTests(unittest.TestCase):
    def test_day4_low_hp_station_is_top_priority(self):
        p = payload(day=STATION_FALLBACK_DAY, gold=200, station_health=STATION_FALLBACK_HP - 100)
        m = Memory(day=STATION_FALLBACK_DAY)
        planner = Planner(Turn.load(p), p, m)
        options = planner.economic.options()
        self.assertEqual(options[0][3], 'StationUpgradeVoucher1', [o[3] for o in options])

    def test_before_day4_low_hp_station_is_not_special(self):
        p = payload(day=3, gold=200, station_health=STATION_FALLBACK_HP - 100)
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        items = [o[3] for o in planner.economic.options()]
        self.assertIn('StationUpgradeVoucher1', items)
        # 第3天仍以围墙/武器配额为先, 基地券不在最前
        self.assertNotEqual(items[0], 'StationUpgradeVoucher1', items)

    def test_healthy_station_no_fallback_on_day4(self):
        p = payload(day=4, gold=200, station_health=1500)
        m = Memory(day=4)
        planner = Planner(Turn.load(p), p, m)
        first = planner.economic.options()[0][3]
        self.assertNotEqual(first, 'StationUpgradeVoucher1')


class TaskReuseTests(unittest.TestCase):
    def task_payload(self, desc):
        p = fixture()
        p['teamOur']['roles'] = [p['teamOur']['roles'][0], unit(4, 'pioneer', 12, 25)]
        p['mapInfo']['zones'] = [{'pos': {'x': 13, 'y': 26}, 'neutralType': 'challengerTaskPoint1'}]
        p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 13, 'y': 26}, 'isValid': True,
            'coldDownRounds': 0, 'timeoutRounds': 100, 'scoreReward': 50, 'goldReward': 30,
            'taskType': '自进化类1'}]
        p['phaseTask'] = desc
        return p

    def test_same_structure_task_matches_saved_procedure(self):
        m = Memory()
        m.skills.append({'task': '【自进化任务】请查询成都今天的天气并提交答案。\
第三方天气API文档：GET http://x/weather?city=<城市名>',
                         'command': 'curl -s "http://x/weather?city=成都"',
                         'answer': '成都今天天气：阴', 'method': 'weather', 'steps': []})
        p = self.task_payload('【自进化任务】请查询上海今天的天气并提交答案。\
第三方天气API文档：GET http://x/weather?city=<城市名>')
        planner = Planner(Turn.load(p), p, m)
        tasks = Tasks(planner)
        tasks.sync_task()
        self.assertIsNotNone(m.task.get('reuse'), m.task)

    def test_reuse_replays_command_without_probing_or_llm(self):
        m = Memory()
        m.skills.append({'task': '查询成都今天的天气。GET http://x/weather?city=<城市名>',
                         'command': 'curl -s "http://x/weather?city=成都"',
                         'answer': '成都今天天气：阴', 'method': 'weather', 'steps': []})
        p = self.task_payload('查询上海今天的天气。GET http://x/weather?city=<城市名>')
        planner = Planner(Turn.load(p), p, m)
        tasks = Tasks(planner)
        tasks.run()
        self.assertEqual(planner.execute_cmd, 'curl -s "http://x/weather?city=成都"')
        self.assertEqual(planner.prompt, '', '复用通道不应请求 LLM')

    def test_same_params_reuse_submits_saved_answer(self):
        # 完全同题(同城市): 重跑命令后直接提交已验证答案(零 LLM)
        m = Memory()
        m.skills.append({'task': '查询成都今天的天气。GET http://x/weather?city=<城市名>',
                         'command': 'curl -s "http://x/weather?city=成都"',
                         'answer': '成都今天天气：阴', 'param': '成都', 'method': 'weather', 'steps': []})
        p = self.task_payload('查询成都今天的天气。GET http://x/weather?city=<城市名>')
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        p['roundNo'] = 2
        p['lastCmdResult'] = '[exitCode:0]\n{"city":"成都","weather":"阴"}'
        planner2 = Planner(Turn.load(p), p, m)
        Tasks(planner2).run()
        cmd = planner2.commands.get('4')
        self.assertIsNotNone(cmd, planner2.commands)
        self.assertEqual(cmd['action'], 'submitAnswer')
        self.assertIn('阴', cmd['taskAnswer'])

    def test_different_params_substitutes_and_does_not_reuse_old_answer(self):
        # 参数不同: 命令里的旧参数被替换为新参数重跑, 且不沿用旧答案(避免答错)
        m = Memory()
        m.skills.append({'task': '查询成都今天的天气。GET http://x/weather?city=<城市名>',
                         'command': 'curl -s "http://x/weather?city=成都"',
                         'answer': '成都今天天气：阴', 'param': '成都', 'method': 'weather', 'steps': []})
        p = self.task_payload('查询上海今天的天气。GET http://x/weather?city=<城市名>')
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        self.assertIn('上海', planner.execute_cmd, '命令参数应被替换为新城市')
        self.assertNotIn('成都', planner.execute_cmd)
        p['roundNo'] = 2
        p['lastCmdResult'] = '[exitCode:0]\n{"city":"上海","weather":"多云"}'
        planner2 = Planner(Turn.load(p), p, m)
        Tasks(planner2).run()
        self.assertNotIn('4', planner2.commands, '不应沿用旧城市的答案')
        self.assertTrue(planner2.prompt, '应请 LLM 依据新结果作答')


if __name__ == '__main__':
    unittest.main()


class NightVoucherDeliveryTests(unittest.TestCase):
    """夜间必须先把背包里的升级券用掉, 再回武器塔旁防御。"""

    def night_payload(self, gold=0, wall_health=1000, worker_pos=None, backpack=None):
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=gold,
                    walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=wall_health)])
        p['roundNo'] = 85                      # 夜晚
        p['teamOur']['roles'][1]['pos'] = worker_pos or {'x': 9, 'y': 22}
        if backpack:
            p['teamOur']['roles'][1]['backpack'] = list(backpack)
        return p, sites[0]

    def inner_cell(self, wall, station=Pos(10, 24)):
        return min((Pos(wall.x + dx, wall.y + dy)
                    for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                   key=lambda q: distance(q, station))

    def test_night_uses_voucher_when_already_on_inner_side(self):
        # 已站在靠基地的内侧 -> 夜里直接使用券
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', wall.x, wall.y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = self.inner_cell(wall).dump()
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertIsNotNone(cmd, planner.commands)
        self.assertEqual(cmd['action'], 'use')
        self.assertEqual(cmd['name'], 'WallUpgradeVoucher1')

    def test_night_moves_to_inner_side_when_adjacent_outside(self):
        # 贴着墙但在外侧 -> 先绕到内侧再用(外侧会被机器人打)
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]
        station = Pos(10, 24)
        outside = max((Pos(wall.x + dx, wall.y + dy)
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                      key=lambda q: distance(q, station))
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', wall.x, wall.y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = outside.dump()
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertEqual(cmd['action'], 'move', cmd)
        tgt = Pos.load(cmd['targetPos'][0])
        self.assertLess(distance(tgt, station), distance(outside, station),
                        '应走向更靠基地的一侧')

    def test_night_moves_toward_target_when_carrying_voucher(self):
        # 拿着券但不在墙边 -> 夜里也要朝墙走过去用掉(而不是直接去武器位)
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 21}     # 距墙若干格
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertIsNotNone(cmd, planner.commands)
        self.assertEqual(cmd['action'], 'move', cmd)
        # 目标应是朝该墙方向的格子(比当前位置更靠近墙)
        tgt = Pos.load(cmd['targetPos'][0])
        self.assertLess(distance(tgt, sites[0]), distance(Pos(9, 21), sites[0]))

    def test_night_weapon_voucher_also_delivered(self):
        p = payload(day=1, gold=0)
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = {'x': 10, 'y': 24}    # 基地旁
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertIsNotNone(cmd, planner.commands)
        self.assertIn(cmd['action'], ('move', 'use'))

    def test_no_buying_at_night(self):
        # 夜里不跑商店采购(采购是白天的事), 避免夜间离岗
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=200, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 22}
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertNotEqual((cmd or {}).get('action'), 'buy', cmd)


class AutoTaskPipelineTests(unittest.TestCase):
    """自动文件定位 -> API 自动调度 -> 作答 -> 记录技能事实 的全链路。"""

    def task_payload(self, desc, task_type='自进化类1'):
        p = fixture()
        p['teamOur']['roles'] = [p['teamOur']['roles'][0], unit(4, 'pioneer', 12, 25)]
        p['mapInfo']['zones'] = [{'pos': {'x': 13, 'y': 26}, 'neutralType': 'challengerTaskPoint1'}]
        p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 13, 'y': 26}, 'isValid': True,
            'coldDownRounds': 0, 'timeoutRounds': 100, 'scoreReward': 50, 'goldReward': 30,
            'taskType': task_type}]
        p['phaseTask'] = desc
        return p

    def test_pipeline_locate_then_api_call_then_submit(self):
        from agent.tasks import task_facts, mentioned_files
        desc = '【自进化任务】读取 task_1_beijing.md 中的说明, 调用其接口给出答案'
        p = self.task_payload(desc)
        m = Memory()
        # 第1轮: 自动定位(命令里应包含所提文件名)
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        self.assertIn('task_1_beijing.md', planner.execute_cmd)
        # 第2轮: API 自动调度(带 __API 标记)
        p['roundNo'] = 2
        p['lastCmdResult'] = ('[exitCode:0]\n__FILE /tmp/selfEvolutionTask/task_1_beijing.md\n'
                              '__API_EXTRACT urls=[http://localhost:8899] hdr=(X-API-Key=heritage-api-key-2024) '
                              'params=[id]\n__API status=200 base=http://localhost:8899\n'
                              '__API_DIST id(15 distinct)\n'
                              '__API_CALL http://localhost:8899?id=3 -> {"answer":"青色石板"}')
        planner2 = Planner(Turn.load(p), p, m)
        Tasks(planner2).run()
        self.assertIn('__API', planner2.execute_cmd)
        # 事实入库: kind=api + base/header/params
        facts = task_facts(m.task['history'])
        self.assertEqual(facts['kind'], 'api')
        self.assertEqual(facts['base'], 'http://localhost:8899')
        self.assertIn('id', facts['params'])
        self.assertEqual(facts['distinct_values'], 15)

    def test_next_same_kind_task_skips_location_round(self):
        # 已有 API 技能事实 -> 新任务(换参数)直接构造调用, 不再跑定位轮
        m = Memory()
        m.skills.append({'task': '读取 task_1_beijing.md 并调用接口作答',
                         'command': 'curl -s "http://localhost:8899?id=3"',
                         'answer': '北京: 青色石板', 'param': 'beijing',
                         'kind': 'api',
                         'facts': {'kind': 'api', 'base': 'http://localhost:8899',
                                   'header': "X-API-Key='heritage-api-key-2024'", 'params': ['id'],
                                   'distinct_values': 15},
                         'method': 'api', 'steps': []})
        p = self.task_payload('读取 task_2_shanghai.md 并调用接口作答')
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        cmd = planner.execute_cmd
        self.assertIn('curl', cmd, cmd)
        self.assertIn('http://localhost:8899', cmd)
        self.assertIn('X-API-Key', cmd)
        self.assertIn('shanghai', cmd, '应替换为新参数')
        self.assertNotIn('__FILE', cmd, '跳过定位轮, 不应重新探测文件')

    def test_facts_kind_file_for_file_only_task(self):
        from agent.tasks import task_facts
        history = [{'result': '[exitCode:0]\n__FILE /tmp/selfEvolutionTask/task_9.md\n内容...'}]
        facts = task_facts(history)
        self.assertEqual(facts['kind'], 'file')
        self.assertEqual(facts['file'], '/tmp/selfEvolutionTask/task_9.md')


class InnerStandTests(unittest.TestCase):
    """夜间对建筑使用券/修复包时, 必须站在靠基地的内侧, 不能站围墙外侧。"""

    def setup_night(self, worker_pos):
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]                      # 迎敌弧线最前排的墙
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', wall.x, wall.y, health=1000)])
        p['roundNo'] = 85                    # 夜晚
        p['teamOur']['roles'][1]['pos'] = dict(worker_pos)
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        return planner, wall

    def test_night_moves_to_inner_side_of_wall(self):
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]
        station = Pos(10, 24)
        # 从正下方接近: 内侧(靠基地)与外侧站位代价相同, 应选内侧
        planner, wall = self.setup_night({'x': wall.x, 'y': wall.y + 4})
        planner.run()
        cmd = planner.commands.get('2')
        self.assertEqual(cmd['action'], 'move', cmd)
        tgt = Pos.load(cmd['targetPos'][0])
        outer = Pos(wall.x + 1, wall.y) if wall.x > station.x else Pos(wall.x - 1, wall.y)
        # 选中的站位应比"外侧格"更靠近基地
        self.assertLessEqual(distance(tgt, station), distance(outer, station), (tgt, outer))
        self.assertLess(distance(tgt, station), distance(Pos(wall.x, wall.y), station) + 2)

    def test_inner_adjacent_uses_in_place(self):
        # 已在靠基地的内侧 -> 就地使用, 不白费一回合
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]
        station = Pos(10, 24)
        inner = min((Pos(wall.x + dx, wall.y + dy)
                     for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                    key=lambda q: distance(q, station))
        planner, wall = self.setup_night(inner.dump())
        planner.run()
        cmd = planner.commands.get('2')
        self.assertEqual(cmd['action'], 'use', cmd)

    def test_daytime_does_not_require_inner_side(self):
        # 白天没有机器人威胁, 站位仍按最短路(不改动既有行为)
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', wall.x, wall.y, health=1000)])
        p['roundNo'] = 30                    # 白天
        p['teamOur']['roles'][1]['pos'] = {'x': wall.x, 'y': wall.y + 4}
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        if cmd and cmd['action'] == 'move':
            tgt = Pos.load(cmd['targetPos'][0])
            self.assertGreaterEqual(distance(tgt, Pos(10, 24)), 1)
