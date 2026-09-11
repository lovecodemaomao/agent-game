"""三项修改的回归测试。

1. 任务探测: 只探测 /tmp/selfEvolutionTask/（不从根目录全盘递归），3 轮固定探测后
   用已知信息请求 LLM 直接给出 API 调用（带 X-API-Key 头、尝试不同参数组合）
2. 矿工分工: 一名工人采石、其余采矿，不出现两人都采石
3. 升级顺序: 第1天武器升级优先；第2天围墙升级优先，围墙升完再武器
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response, wall_sites
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent.tasks import PROBES, PROBE_LIMIT, HERITAGE_API_KEY, TASK_DIR


def task_payload():
    p = fixture()
    p['teamOur']['roles'] = [p['teamOur']['roles'][0], unit(4, 'pioneer', 12, 25)]
    p['mapInfo']['zones'] = [{'pos': {'x': 13, 'y': 26}, 'neutralType': 'challengerTaskPoint1'}]
    p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 13, 'y': 26}, 'isValid': True,
        'coldDownRounds': 0, 'timeoutRounds': 100, 'scoreReward': 50, 'goldReward': 30,
        'taskType': '自进化类1'}]
    return p


def two_worker_payload(day=1, gold=200, zones=None):
    p = fixture()
    p['roundNo'] = (day - 1) * 130 + 1
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = [
        unit(1, 'station', 10, 24),
        unit(2, 'worker', 9, 24, capacity=100),
        unit(3, 'worker', 12, 24, capacity=100),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ]
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = [{'name': n, 'price': v} for n, v in [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30), ('WallFixer', 10)]]
    return p


class ProbeTests(unittest.TestCase):
    def test_first_probe_targets_task_dir_without_root_recursion(self):
        probe = PROBES[0]
        self.assertIn('find', probe)
        self.assertIn(TASK_DIR, probe)
        # 禁止从根目录全盘递归（会拖爆 15 秒沙盒限制）
        self.assertNotIn('find / ', probe)
        self.assertNotIn("find / ", PROBES[1])
        for probe in PROBES:
            self.assertIn(TASK_DIR, probe)
            self.assertNotIn("-maxdepth 99", probe)

    def test_three_fixed_probes_then_knowledge_prompt(self):
        p = task_payload()
        p['phaseTask'] = '【自进化任务】调用遗产接口查询文物编号'
        m = Memory()
        rounds = []
        n = 1
        for _ in range(10):
            p['roundNo'] = n
            r = decide_response(p, m)
            if r['prompt']:
                rounds.append(('prompt', r['prompt']))
                break
            self.assertTrue(r['executeCmd'], r)
            rounds.append(('probe', r['executeCmd']))
            p['lastCmdResult'] = '[exitCode:0]\nfile: task.txt api=http://127.0.0.1:9/heritage'
            n += 1
        probes = [c for kind, c in rounds if kind == 'probe']
        self.assertEqual(len(probes), PROBE_LIMIT, probes)
        self.assertEqual(probes, list(PROBES))
        prompt = rounds[-1][1]
        # 知识型 prompt: 带上前 3 轮探测结果, 并要求带鉴权头的 API 调用
        self.assertIn('probe_results', prompt)
        self.assertIn(HERITAGE_API_KEY, prompt)
        self.assertIn('curl', prompt)
        self.assertIn('参数组合', prompt)
        self.assertEqual(m.llm_used, 0)      # 任务期不占每日额度

    def test_probe_output_forwarded_to_llm(self):
        p = task_payload()
        p['phaseTask'] = '读文件任务'
        m = Memory()
        n = 1
        for _ in range(PROBE_LIMIT):
            p['roundNo'] = n
            decide_response(p, m)
            self.assertIn('selfEvolutionTask', m.task['history'][-1]['command'])
            p['lastCmdResult'] = '[exitCode:0]\nUNIQUE_PROBE_MARKER_%d' % n
            n += 1
        p['roundNo'] = n
        r = decide_response(p, m)
        self.assertIn('UNIQUE_PROBE_MARKER_%d' % (n - 1), r['prompt'])


class MiningRoleTests(unittest.TestCase):
    def test_one_worker_mines_stone_and_the_other_mines_ore(self):
        p = two_worker_payload(day=1)
        m = Memory()
        planner = Planner(Turn.load(p), p, m)
        eco = planner.economic
        workers = sorted(planner.turn.workers(), key=lambda r: r.unit_id)
        kinds = [eco.wanted_kinds(w) for w in workers]
        self.assertEqual(kinds, [('stone',), ('iron', 'copper')], kinds)
        # 目标矿种确实按分工挑选
        chosen = []
        for w, allowed in zip(workers, kinds):
            routes = planner.route(w)
            cands = eco.mining_candidates(w, routes, allowed)
            self.assertTrue(cands, f'worker {w.unit_id} 应有可用矿点')
            chosen.append({c['kind'] for c in cands})
        self.assertEqual(chosen[0], {'stone'})
        self.assertTrue(chosen[1] <= {'iron', 'copper'})

    def test_both_workers_never_mine_stone_when_walls_missing(self):
        p = two_worker_payload(day=1)
        m = Memory()
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertEqual(sum(k == ('stone',) for k in kinds), 1, kinds)

    def test_all_workers_move_to_ore_once_walls_complete(self):
        p = two_worker_payload(day=2)
        p['mapInfo']['zones'] = [
            {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
            {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
            {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
        ]
        # 按真实墙位把迎敌半圈建满；墙齐后不需要专人采石
        sites = wall_sites(Turn.load(p))
        self.assertTrue(sites)
        p['teamOur']['roles'] += [unit(40 + i, 'wall', q.x, q.y)
                                  for i, q in enumerate(sites)]
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('iron', 'copper') for k in kinds), kinds)


class UpgradeOrderTests(unittest.TestCase):
    def planned(self, day):
        p = two_worker_payload(day=day, gold=400)
        p['teamOur']['roles'] += [unit(30, 'wall', 13, 24)]
        m = Memory(day=day)
        planner = Planner(Turn.load(p), p, m)
        return planner, planner.economic.options()

    def test_day1_prefers_weapon_upgrades(self):
        planner, options = self.planned(day=1)
        first = options[0]
        self.assertTrue(first[3].startswith('Weapon'), [o[3] for o in options])
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        self.assertLess(weapon_pos, wall_pos)

    def test_day2_wall_phase_first_then_weapons(self):
        planner, options = self.planned(day=2)
        walls = [o for o in options if o[3].startswith('Wall')]
        self.assertTrue(walls, '存在待升级围墙')
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        self.assertLess(wall_pos, weapon_pos, [o[3] for o in options])

    def test_day2_weapons_resume_after_wall_phase_done(self):
        # 主要围墙已升到2级后 -> 武器升级接管
        p = two_worker_payload(day=2, gold=400)
        sites = wall_sites(Turn.load(p))
        p['teamOur']['roles'] += [unit(40 + i, 'wall', q.x, q.y, level=(2 if i < len(sites) - 1 else 1))
                                  for i, q in enumerate(sites)]
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        options = planner.economic.options()
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        self.assertLess(weapon_pos, wall_pos, [o[3] for o in options])

    def test_day1_weapon_voucher_not_blocked_by_missing_walls(self):
        # 第一天缺墙时，武器升级券仍可采购（此前会被 priority>=1 的守卫全部拦掉）
        p = two_worker_payload(day=1, gold=200)
        p['teamOur']['roles'] = [u for u in p['teamOur']['roles']
                                 if u['roleType'] in ('station', 'worker', 'rocket', 'railgun')]
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        bought = [j for j in m.jobs.values() if j.get('type') == 'upgrade']
        self.assertTrue(bought, m.jobs)
        self.assertTrue(bought[0]['item'].startswith('Weapon'), bought[0])

    def test_damaged_wall_repair_keeps_top_priority_on_both_days(self):
        for day in (1, 2):
            p = two_worker_payload(day=day, gold=400)
            p['teamOur']['roles'] += [unit(30, 'wall', 13, 24, health=200, level=3)]
            m = Memory(day=day)
            planner = Planner(Turn.load(p), p, m)
            options = planner.economic.options()
            # 三级墙已满级，保命手段是 WallFixer（修复包），应排在首位
            self.assertEqual(options[0][3], 'WallFixer', [o[3] for o in options])


if __name__ == '__main__':
    unittest.main()
