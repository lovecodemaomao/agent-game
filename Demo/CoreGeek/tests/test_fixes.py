"""三项逻辑修复的回归测试。

1. 开拓者接任务/解题/提交
2. 采矿就近优先
3. 升级券与修墙包真正采购落地
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent.brain as brain
from agent.brain import Planner, decide, respond
from agent.protocol import Turn, Pos, distance


class ResetProcessState(unittest.TestCase):
    """进程级状态（黏性矿点/失败记忆/任务代理）在每个用例前重置，避免相互干扰。"""

    def setUp(self):
        brain.STICKY_MINE.clear()
        brain.FAILED_ACTIONS.clear()
        brain._LAST_COMMAND.clear()
        brain._TASK_AGENT.task = None
        brain._TASK_AGENT.learned.clear()


def unit(uid, kind, x, y, **kw):
    return {'id': uid, 'roleType': kind, 'pos': {'x': x, 'y': y},
            'health': 1500 if kind == 'station' else (1000 if kind in ('wall', 'rocket', 'railgun') else 220),
            'level': 1, 'backpack': [], 'backPackCapability': 100, **kw}


def fixture(**over):
    payload = {'roundNo': 1, 'mapInfo': {'width': 41, 'height': 32, 'zones': []},
               'teamOur': {'type': 'challenger', 'goldNum': 75, 'playerTasks': [], 'roles': [
                   unit(1, 'station', 10, 24), unit(2, 'worker', 9, 24),
                   unit(3, 'worker', 12, 24), unit(4, 'pioneer', 10, 25)]},
               'teamEnemy': {'roles': []}, 'robot': {'roles': []},
               'vendorShopList': [{'name': k, 'price': v} for k, v in
                                  [('stone', 1), ('iron', 3), ('copper', 5)]],
               'weaponShopList': [], 'phaseTask': '',
               'lastCmdResult': '', 'llmResp': '', 'lastRoundRoleActionResults': {}}
    payload.update(over)
    return payload


class FixOnePioneerTask(ResetProcessState):
    def test_pioneer_walks_to_and_accepts_task(self):
        p = fixture()
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(2, 'worker', 9, 24),
                                 unit(4, 'pioneer', 14, 15)]
        p['teamOur']['playerTasks'] = [{'taskType': '自进化类1', 'taskPosition': {'x': 14, 'y': 14},
                                        'coldDownRounds': 0, 'isValid': True, 'timeoutRounds': 100,
                                        'scoreReward': 50, 'goldReward': 30}]
        commands = decide(p)
        self.assertEqual(commands['4'], {'action': 'acceptTask'})

    def test_pioneer_skips_task_when_cooling_down(self):
        p = fixture()
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(4, 'pioneer', 14, 15)]
        p['teamOur']['playerTasks'] = [{'taskType': '自进化类1', 'taskPosition': {'x': 14, 'y': 14},
                                        'coldDownRounds': 7, 'isValid': False, 'timeoutRounds': 100,
                                        'scoreReward': 50, 'goldReward': 30}]
        self.assertNotIn('4', decide(p))

    def test_pioneer_probes_and_submits(self):
        task = ('【自进化任务】请查询成都今天的天气并提交答案。\n'
                '第三方天气API文档：GET http://127.0.0.1:12345/weather?city=<城市名>\n'
                '返回 JSON：{"city": "...", "weather": "..."}')
        p = fixture(phaseTask=task)
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(4, 'pioneer', 14, 14)]
        result = respond(p)
        self.assertIn('curl', result['executeCmd'])       # 沙盒探测
        self.assertIn('curl', result['prompt'])           # 平台 LLM 通道
        # 次回合带回沙盒结果 -> 提交答案
        p['lastCmdResult'] = '[exitCode:0]\n{"city": "成都", "weather": "阴"}'
        result = respond(p)
        self.assertEqual(result['roleCommandMap']['4']['action'], 'submitAnswer')
        self.assertIn('阴', result['roleCommandMap']['4']['taskAnswer'])

    def test_pioneer_sop_reuse_needs_no_llm(self):
        _TASK_AGENT = brain._TASK_AGENT
        task = ('【自进化任务】请查询北京今天的天气并提交答案。\n'
                '第三方天气API文档：GET http://127.0.0.1:12345/weather?city=<城市名>\n'
                '返回 JSON：{"city": "...", "weather": "..."}')
        _TASK_AGENT.learned.add('weather')                # 模拟同类任务已学会
        _TASK_AGENT.task = None
        p = fixture(phaseTask=task)
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(4, 'pioneer', 14, 14)]
        result = respond(p)
        self.assertEqual(result['prompt'], '')            # SOP 通道零 LLM
        self.assertIn('bash sop_weather.sh', result['executeCmd'])


class FixTwoMining(ResetProcessState):
    def test_picks_nearest_mine_even_if_cheaper(self):
        # 近处石矿(1金, 1格) vs 远处铜矿(5金, 12格) -> 应该选近处
        p = fixture()
        p['mapInfo']['zones'] = [
            {'pos': {'x': 9, 'y': 25}, 'neutralType': 'stone'},
            {'pos': {'x': 22, 'y': 14}, 'neutralType': 'copper'},
            {'pos': {'x': 8, 'y': 24}, 'neutralType': 'vendor'}]
        planner = Planner(Turn.load(p), p)
        role = planner.turn.workers()[0]
        planner.economy(role, planner.route(role))
        command = planner.commands['2']
        mine = command['targetPos'][0]
        self.assertEqual(mine, {'x': 9, 'y': 25}, '应就近采集石矿而不是远征铜矿')

    def test_prefers_higher_price_when_travel_is_close(self):
        # 两矿行程接近(差1格) -> 选高价铜矿
        p = fixture()
        p['mapInfo']['zones'] = [
            {'pos': {'x': 9, 'y': 25}, 'neutralType': 'stone'},
            {'pos': {'x': 8, 'y': 25}, 'neutralType': 'copper'},
            {'pos': {'x': 8, 'y': 24}, 'neutralType': 'vendor'}]
        planner = Planner(Turn.load(p), p)
        role = planner.turn.workers()[0]
        planner.economy(role, planner.route(role))
        self.assertEqual(planner.commands['2']['targetPos'][0], {'x': 8, 'y': 25})


class FixThreeUpgrades(ResetProcessState):
    def test_buys_wall_upgrade_voucher(self):
        p = fixture()
        p['teamOur']['goldNum'] = 40
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(2, 'worker', 9, 24),
                                 unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23),
                                 unit(12, 'railgun', 11, 25), unit(30, 'wall', 13, 24)]
        p['mapInfo']['zones'] = [{'pos': {'x': 8, 'y': 24}, 'neutralType': 'weaponShop'}]
        p['weaponShopList'] = [{'name': 'WallUpgradeVoucher1', 'price': 20}]
        planner = Planner(Turn.load(p), p)
        role = planner.turn.workers()[0]
        self.assertTrue(planner.procure_upgrade(role, planner.route(role)))
        self.assertEqual(planner.commands['2'], {'action': 'buy', 'name': 'WallUpgradeVoucher1', 'num': 1})

    def test_weapon_upgrade_still_has_priority_over_wall(self):
        p = fixture()
        p['teamOur']['goldNum'] = 120
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(2, 'worker', 9, 24),
                                 unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23),
                                 unit(12, 'railgun', 11, 25), unit(30, 'wall', 13, 24)]
        p['mapInfo']['zones'] = [{'pos': {'x': 8, 'y': 24}, 'neutralType': 'weaponShop'}]
        p['weaponShopList'] = [{'name': 'WeaponUpgradeVoucher1', 'price': 100},
                               {'name': 'WallUpgradeVoucher1', 'price': 20}]
        planner = Planner(Turn.load(p), p)
        role = planner.turn.workers()[0]
        self.assertTrue(planner.procure_upgrade(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['name'], 'WeaponUpgradeVoucher1')

    def test_repairs_damaged_wall_with_carried_fixer(self):
        p = fixture()
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24),
                                 unit(2, 'worker', 13, 25, backpack=['WallFixer']),
                                 unit(30, 'wall', 13, 24, health=150)]
        planner = Planner(Turn.load(p), p)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertTrue(planner.repair_wall(role, planner.route(role)))
        self.assertEqual(planner.commands['2'], {'action': 'use', 'name': 'WallFixer',
                                                 'targetPos': [{'x': 13, 'y': 24}]})

    def test_buys_wall_fixer_when_wall_is_low(self):
        p = fixture()
        p['teamOur']['goldNum'] = 15
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(2, 'worker', 9, 24),
                                 unit(30, 'wall', 13, 24, health=120)]
        p['mapInfo']['zones'] = [{'pos': {'x': 8, 'y': 24}, 'neutralType': 'weaponShop'}]
        p['weaponShopList'] = [{'name': 'WallFixer', 'price': 10}]
        planner = Planner(Turn.load(p), p)
        role = planner.turn.workers()[0]
        self.assertTrue(planner.repair_wall(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['name'], 'WallFixer')

    def test_healthy_wall_needs_no_repair(self):
        p = fixture()
        p['teamOur']['roles'] = [unit(1, 'station', 10, 24), unit(2, 'worker', 13, 25),
                                 unit(30, 'wall', 13, 24, health=1000)]
        planner = Planner(Turn.load(p), p)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.repair_wall(role, planner.route(role)))


if __name__ == '__main__':
    unittest.main()
