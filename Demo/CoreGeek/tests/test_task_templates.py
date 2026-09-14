"""任务执行效率(自动定位/预设模板/立即提交/死线管理) + 三条战术要求的回归测试。

1. 修复部署类: 一条命令建目录/改配置/加权限/去 Windows 回车/跑检查, 并从输出直接取 TOKEN
2. 查询类: 一条命令试完 鉴权头 x 参数名 组合并翻页取全量, 依据记录直接算出答案
3. 死线管理: 剩余时限不够时不接任务
4. 人物不得占用待建围墙格(会在那一圈站住导致建不了墙)
5. 开拓者无任务时去商店旁待命并按计划买券, 天黑前回武器塔
6. 第3天之后基地受伤 -> 当天第一优先级是基地升级券
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_strategy import fixture, unit
from agent import templates
from agent.brain import Planner, decide_response, wall_sites
from agent.memory import Memory
from agent.protocol import Pos, Turn, distance
from agent.tasks import Tasks

SHELL = '/bin/zsh' if os.path.exists('/bin/zsh') else '/bin/sh'

ENGINEER_DESC = '''进入 %s 目录，修复以下问题：
1. 创建 logs/alpha 目录
2. 修改 `config/alpha.conf` 第3行为 `port 8080`
3. 修改 `config/alpha.conf` 第6行为 `name alpha-app`
4. 设置 bin/start.sh 可执行权限
5. 去除 check 脚本的 Windows 回车符 (\\r)
6. 运行 ./check 验证并获取 TOKEN'''

API_DESC = ('【自进化任务】读取 /tmp/selfEvolutionTask/1-fixed-step/1-api-query/ws_1/task_1_beijing.md，'
            '调用 {base}/api/v1/heritage/search 查询北京市全部文化遗产记录，'
            '其中保护级别为“世界遗产”的数量需单独统计；'
            '按要求提交 `city`、`total_count`、`world_heritage_count`、`types`、`oldest_era`。')

RECORDS = [
    {'name': '故宫', 'city': '北京', 'protection_level': '世界遗产', 'type': '古建筑', 'era': '明清'},
    {'name': '长城', 'city': '北京', 'protection_level': '世界遗产', 'type': '古建筑', 'era': '明清'},
    {'name': '周口店遗址', 'city': '北京', 'protection_level': '世界遗产', 'type': '古遗址', 'era': '旧石器'},
    {'name': '天坛', 'city': '北京', 'protection_level': '世界遗产', 'type': '古建筑', 'era': '明清'},
    {'name': '颐和园', 'city': '北京', 'protection_level': '世界遗产', 'type': '古建筑', 'era': '清'},
    {'name': '明十三陵', 'city': '北京', 'protection_level': '世界遗产', 'type': '古墓葬', 'era': '明'},
    {'name': '卢沟桥', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '金'},
    {'name': '潭柘寺', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '晋'},
    {'name': '戒台寺', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '隋'},
    {'name': '云居寺', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '唐'},
    {'name': '法源寺', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '唐'},
    {'name': '雍和宫', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '清'},
    {'name': '大觉寺', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '辽'},
    {'name': '红螺寺', 'city': '北京', 'protection_level': '国家级', 'type': '古建筑', 'era': '唐'},
    {'name': '琉璃河遗址', 'city': '北京', 'protection_level': '国家级', 'type': '古遗址', 'era': '西周'},
]
EXPECTED_TYPES = ['古建筑', '古遗址', '古墓葬']


def run_shell(command):
    return subprocess.run([SHELL, '-c', command], capture_output=True,
                          encoding='utf-8', errors='replace', timeout=60)


def write_workspace(root):
    """搭一个和比赛环境同形的任务工作区: 配置待改、start.sh 无执行位、check 带 CRLF。"""
    ws = Path(root) / 'ws_1'
    (ws / 'config').mkdir(parents=True)
    (ws / 'bin').mkdir(parents=True)
    conf = ws / 'config' / 'alpha.conf'
    conf.write_text('server\nmode dev\nport 9090\nnamespace demo\nlog info\nname alpha-dev\n')
    start = ws / 'bin' / 'start.sh'
    start.write_text('#!/bin/sh\necho started\n')
    start.chmod(0o644)
    check = ws / 'check'
    body = ('#!/bin/sh\nok=1\n'
            '[ -d logs/alpha ] || ok=0\n'
            '[ "$(sed -n 3p config/alpha.conf)" = "port 8080" ] || ok=0\n'
            '[ "$(sed -n 6p config/alpha.conf)" = "name alpha-app" ] || ok=0\n'
            '[ -x bin/start.sh ] || ok=0\n'
            'if [ $ok -eq 1 ]; then\n'
            '  echo "[ OK ] 全部通过 (6/6)"\n'
            '  echo "TOKEN: fc1e78eb2a5a"\n'
            'else\n'
            '  echo "[FAIL]"\n'
            'fi\n')
    check.write_bytes(body.replace('\n', '\r\n').encode())     # Windows 回车
    (ws / 'task_1_alpha.md').write_text(ENGINEER_DESC % str(ws))
    return ws


class EngineerTemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='taskws-')
        self.ws = write_workspace(self.root)
        self.desc = ENGINEER_DESC % (str(self.ws) + '/')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_parses_steps_into_single_command(self):
        command = templates.engineer_command(self.desc)
        self.assertIn('mkdir -p logs/alpha', command)
        self.assertIn("'3s|.*|port 8080|'", command)
        self.assertIn("'6s|.*|name alpha-app|'", command)
        self.assertIn('chmod 755 bin/start.sh', command)
        # 关键: 跑检查脚本之前必须先去 Windows 回车
        self.assertIn("sed -i.bak 's/\\r$//' check", command)
        self.assertLess(command.index("sed -i.bak 's/\\r$//' check"),
                        command.index('sh ./check'))
        self.assertEqual(templates.estimate(self.desc), templates.ENGINEER_ROUNDS)

    def test_check_script_has_crlf_so_stripping_is_required(self):
        naive = templates.engineer_command(self.desc).replace(
            "sed -i.bak 's/\\r$//' check", 'true')
        output = run_shell(naive).stdout
        self.assertNotIn('TOKEN', output, '未去 CR 的检查脚本不应通过(证明用例有区分度)')

    def test_preset_command_fixes_and_returns_token_in_one_round(self):
        result = run_shell(templates.engineer_command(self.desc))
        self.assertIn('[ OK ] 全部通过 (6/6)', result.stdout)
        self.assertIn('CHECK_EXIT=0', result.stdout)
        self.assertTrue((self.ws / 'logs' / 'alpha').is_dir())
        self.assertTrue((self.ws / 'bin' / 'start.sh').stat().st_mode & stat.S_IXUSR)
        self.assertEqual(templates.derive_answer(self.desc, '', result.stdout), 'fc1e78eb2a5a')

    def test_task_finishes_in_two_rounds_without_llm(self):
        """任务生效 -> 一条预设命令 -> 结果即答案 -> 立刻提交(共 2 回合, 0 次 LLM)。"""
        payload = task_payload(self.desc)
        memory = Memory()
        first = decide_response(payload, memory)
        self.assertIsNotNone(memory.task, memory.trace)
        self.assertIn('mkdir -p logs/alpha', first['executeCmd'])
        self.assertNotIn('logs/alpha', first['prompt'])
        self.assertEqual(memory.llm_used, 0)
        payload['roundNo'] = 2
        payload['lastCmdResult'] = ('[exitCode:0]\n[ OK ] 全部通过 (6/6)\n'
                                    'TOKEN: fc1e78eb2a5a')
        second = decide_response(payload, memory)
        command = second['roleCommandMap']['4']
        self.assertEqual(command['action'], 'submitAnswer')
        self.assertEqual(command['taskAnswer'], 'fc1e78eb2a5a')
        self.assertEqual(second['prompt'], '')


class ApiTemplateTests(unittest.TestCase):
    server = None
    requests = []

    @classmethod
    def setUpClass(cls):
        cls.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ApiTemplateTests.requests.append(dict(self.headers))
                if not self.path.startswith('/api/v1/heritage/search'):
                    self.send_error(404)
                    return
                if self.headers.get('Authorization') != 'Bearer heritage-api-key-2024':
                    self.send_error(401)
                    return
                query = self.path.split('?', 1)[1] if '?' in self.path else ''
                if 'location=' not in query and 'city=' not in query:
                    self.send_error(400)
                    return
                body = json.dumps({'code': 0, 'data': {'records': RECORDS}},
                                  ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        ApiTemplateTests.requests = []
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.desc = API_DESC.format(base=self.base)

    def test_command_tries_auth_and_param_combinations(self):
        command = templates.api_command(self.desc, '')
        self.assertIsNotNone(command)
        self.assertIn('BASE=', command)
        self.assertIn('PATHS=', command)
        self.assertEqual(templates.estimate(self.desc), templates.API_ROUNDS)

    def test_harvest_uses_bearer_auth_and_location_param(self):
        result = run_shell(templates.api_command(self.desc, ''))
        harvest = templates.parse_harvest(result.stdout)
        self.assertIsNotNone(harvest, result.stdout)
        self.assertEqual(harvest['status'], 'OK')
        self.assertEqual(harvest['auth'], 'Authorization')      # 预置正确鉴权头
        self.assertEqual(harvest['param'], 'location')          # 预置正确参数名
        self.assertEqual(harvest['count'], len(RECORDS))
        self.assertTrue(any('Bearer heritage-api-key-2024' in str(h)
                            for h in ApiTemplateTests.requests))

    def test_answer_derived_from_records_without_llm(self):
        result = run_shell(templates.api_command(self.desc, ''))
        answer = json.loads(templates.derive_answer(self.desc, '', result.stdout))
        self.assertEqual(answer['city'], '北京')
        self.assertEqual(answer['total_count'], len(RECORDS))
        self.assertEqual(answer['world_heritage_count'], 6)
        self.assertEqual(answer['types'], EXPECTED_TYPES)
        self.assertEqual(answer['oldest_era'], '周口店遗址')

    def test_task_submits_derived_answer_in_two_rounds(self):
        payload = task_payload(self.desc)
        memory = Memory()
        first = decide_response(payload, memory)
        self.assertIn('harvest.py', first['executeCmd'])
        payload['roundNo'] = 2
        payload['lastCmdResult'] = run_shell(templates.api_command(self.desc, '')).stdout
        second = decide_response(payload, memory)
        command = second['roleCommandMap']['4']
        self.assertEqual(command['action'], 'submitAnswer')
        answer = json.loads(command['taskAnswer'])
        self.assertEqual(answer['world_heritage_count'], 6)
        self.assertEqual(second['prompt'], '', '记录已足够作答时不应再请求 LLM')

    def test_unprovable_schema_falls_back_to_llm(self):
        records = [{'name': '甲', 'city': '北京', 'protection_level': '世界遗产',
                    'type': '古建筑', 'era': '唐'}]
        # 未知字段 -> 不硬猜, 交给 LLM
        self.assertIsNone(templates.answer_from_harvest(['mystery_field'], records, '北京', '题干'))
        # *_count 的定语在题干里找不到对应取值 -> 不硬猜
        self.assertIsNone(templates.answer_from_harvest(['dragon_count'], records, '北京', '题干'))
        # 记录不是字典(接口返回裸值) -> 不硬猜
        self.assertIsNone(templates.answer_from_harvest(['total_count'], [1, 2, 3], '北京', '题干'))


class DocQueryTests(unittest.TestCase):
    """题干自带接口文档(GET url?city=<城市名>)的任务: 按文档原样调用一次, 不猜参数。"""

    server = None

    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = self.path.split('?', 1)[1] if '?' in self.path else ''
                params = dict(p.split('=', 1) for p in query.split('&') if '=' in p)
                city = urllib.parse.unquote(params.get('city', ''))
                body = json.dumps({'city': city, 'weather': '阴'}, ensure_ascii=False).encode()
                self.send_response(200 if city else 400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.desc = ('【自进化任务】请查询成都今天的天气并提交答案。\n'
                     '第三方天气API文档：GET %s/weather?city=<城市名>\n'
                     '返回 JSON：{"city": "...", "weather": "..."}\n'
                     '完成后调用 submitAnswer，答案中需包含该城市的天气现象。' % base)

    def test_documented_call_is_not_mistaken_for_record_harvest(self):
        # 单对象接口(不是记录列表) -> 不走采集模板, 避免白花一个回合
        self.assertIsNone(templates.api_command(self.desc, ''))
        command = templates.doc_query_command(self.desc, '')
        self.assertIsNotNone(command)
        self.assertTrue(command.startswith('curl -s -G "'))
        self.assertIn('--data-urlencode "city=成都"', command)
        self.assertLessEqual(templates.estimate(self.desc), templates.API_ROUNDS)

    def test_documented_call_reaches_api_in_one_round(self):
        result = run_shell(templates.doc_query_command(self.desc, ''))
        self.assertIn('成都', result.stdout)
        self.assertIn('阴', result.stdout)

    def test_successful_preset_result_skips_the_probe_phase(self):
        # 预设调用已经拿到数据 -> 不再花 3 个回合做目录探测, 直接进入知识 prompt
        from agent.tasks import result_looks_ok
        self.assertTrue(result_looks_ok('{"city": "成都", "weather": "阴"}'))
        self.assertFalse(result_looks_ok('[exitCode:0]\n[FAIL]'))
        self.assertFalse(result_looks_ok('sh: check: No such file or directory'))
        payload = task_payload(self.desc)
        memory = Memory()
        first = decide_response(payload, memory)
        self.assertIn('--data-urlencode', first['executeCmd'])
        payload['roundNo'] = 2
        payload['lastCmdResult'] = '[exitCode:0]\n{"city": "成都", "weather": "阴"}'
        second = decide_response(payload, memory)
        self.assertEqual(second['executeCmd'], '', '成功结果之后不应再探测')
        self.assertIn('history', second['prompt'])

    def test_flat_response_is_not_auto_submitted(self):
        # 单对象响应没有可证明的答题字段 -> 交回 LLM 组织答案, 不硬猜格式
        harvest = '__API {"status": "OK", "records": [], "flat": {"city": "成都", "weather": "阴"}}'
        self.assertIsNone(templates.derive_answer(self.desc, '', harvest))

    def test_documented_param_is_tried_first_by_harvest(self):
        desc = ('【自进化任务】查询北京市全部文化遗产记录。\n'
                '接口示例：GET http://localhost:8899/api/v1/heritage/search?location=北京&limit=100\n'
                '提交 `city`、`total_count`。')
        command = templates.api_command(desc, '')
        self.assertIsNotNone(command)
        self.assertIn("PARAMS='location", command)


class DeadlineTests(unittest.TestCase):
    def offer(self, timeout):
        """任务尚未接单(phaseTask 为空), 只有任务点报价。"""
        desc = ENGINEER_DESC % '/tmp/selfEvolutionTask/x/ws_9/'
        payload = task_payload(desc)
        payload['phaseTask'] = ''
        payload['teamOur']['playerTasks'][0]['timeoutRounds'] = timeout
        return payload

    def test_task_with_too_little_time_is_not_accepted(self):
        memory = Memory()
        response = decide_response(self.offer(2), memory)      # 只剩 2 回合
        self.assertNotIn('acceptTask', [c['action'] for c in response['roleCommandMap'].values()])
        self.assertTrue(any('offer skipped' in line for line in memory.trace), memory.trace)

    def test_task_with_enough_time_is_accepted(self):
        response = decide_response(self.offer(60), Memory())
        self.assertEqual(response['roleCommandMap']['4']['action'], 'acceptTask')


class ParkingCellTests(unittest.TestCase):
    """需求1: 任何人不得站在"待建围墙格"上(会在那一圈占位, 导致围墙建不起来)。"""

    def day_payload(self, walls=()):
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=200, walls=list(walls))
        return p, sites

    def test_unbuilt_wall_cells_are_excluded_from_pathing(self):
        p, sites = payload_and_sites(self.day_payload()[0])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        parking = planner.parking_cells()
        self.assertTrue(set(sites) <= parking,
                        sorted(parking, key=lambda q: (q.x, q.y)))
        worker = planner.turn.workers()[0]
        routes = planner.route(worker)
        for site in sites:
            self.assertNotIn(site, routes.cost, '待建围墙格不应出现在可达格集合里')

    def test_built_wall_cells_are_not_parking_cells(self):
        p, sites = self.day_payload()
        p['teamOur']['roles'].append(unit(40, 'wall', sites[0].x, sites[0].y, health=1000))
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        self.assertNotIn(sites[0], planner.parking_cells())
        self.assertIn(sites[1], planner.parking_cells())

    def test_nobody_ever_moves_onto_a_pending_wall_cell(self):
        # 把工人摆在待建围墙格上(最坏情况), 一轮之内它必须离开这一圈
        p, sites = self.day_payload()
        p['teamOur']['roles'][1]['pos'] = sites[2].dump()
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        parking = planner.parking_cells()
        for uid, command in planner.commands.items():
            if command['action'] != 'move':
                continue
            target = Pos.load(command['targetPos'][0])
            self.assertNotIn(target, parking, f'role {uid} 停在待建围墙格 {target}')

    def test_tower_operators_do_not_stand_in_the_wall_ring(self):
        p, sites = self.day_payload()
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        parking = planner.parking_cells()
        for role, tower in planner.assign_towers(day=True) or []:
            self.assertNotIn(role.pos, parking)


class ShopStandbyTests(unittest.TestCase):
    """需求2: 开拓者没任务时去商店旁待命并按计划买券。"""

    def pioneer_payload(self, day=1, gold=250, pos=None):
        p = payload(day=day, gold=gold)
        p['teamOur']['roles'].append(unit(4, 'pioneer', *(pos or (14, 20))))
        p['teamOur']['playerTasks'] = []
        p['phaseTask'] = ''
        return p

    def test_walks_toward_shop_when_idle(self):
        p = self.pioneer_payload()
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        pioneer = next(r for r in planner.turn.alive(('pioneer',)))
        shop = next(q for q, k in planner.turn.zones.items() if k == 'weaponShop')
        self.assertTrue(planner.pioneer_standby())
        command = planner.commands['4']
        self.assertEqual(command['action'], 'move')
        target = Pos.load(command['targetPos'][0])
        self.assertLess(distance(target, shop), distance(pioneer.pos, shop))

    def test_buys_plan_items_once_standing_at_the_shop(self):
        p = self.pioneer_payload(pos=(7, 25))          # 商店(7,24)旁边
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        self.assertTrue(planner.pioneer_standby())
        command = planner.commands['4']
        self.assertEqual(command['action'], 'buy', command)
        self.assertIn(command['name'], ('WeaponUpgradeVoucher1', 'WeaponUpgradeVoucher2'))

    def test_no_standby_shopping_while_a_task_is_active(self):
        desc = ENGINEER_DESC % '/tmp/selfEvolutionTask/x/ws_1/'
        p = task_payload(desc)
        p['teamOur']['roles'].append(unit(4, 'pioneer', 12, 25))
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).sync_task()
        self.assertIsNotNone(m.task)
        self.assertFalse(planner.pioneer_standby())

    def test_returns_home_before_dark_instead_of_shopping(self):
        # 第 70 回合起只剩 6 回合: 开拓者必须一路回家, 不再去商店下单
        p = self.pioneer_payload(pos=(14, 20))
        m = Memory(day=1)
        for n in range(70, 78):
            p['roundNo'] = n
            response = decide_response(p, m)
            command = response['roleCommandMap'].get('4')
            self.assertNotEqual((command or {}).get('action'), 'buy',
                                '天黑前不该再去商店买东西')
            roles = {str(u['id']): u for u in p['teamOur']['roles']}
            if command and command['action'] == 'move':
                roles['4']['pos'] = command['targetPos'][0]
            pos = Pos.load(roles['4']['pos'])
            towers = [Pos.load(u['pos']) for u in p['teamOur']['roles']
                      if u['roleType'] in ('rocket', 'railgun', 'gatling')]
            if min(distance(pos, t) for t in towers) <= 1:
                return                      # 天黑前已回到武器塔旁
        self.fail('开拓者未在天黑前回到武器塔旁')

    def test_no_shopping_at_night(self):
        p = self.pioneer_payload(pos=(7, 25))
        p['roundNo'] = 85
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        self.assertFalse(planner.economic.shop_standby(
            next(r for r in planner.turn.alive(('pioneer',))), planner.route(
                next(r for r in planner.turn.alive(('pioneer',))))))


class StationUrgentPurchaseTests(unittest.TestCase):
    """需求3: 第3天之后基地受伤 -> 当天第一优先级去买基地升级券。"""

    def test_prepare_assigns_station_voucher_purchase_on_day3(self):
        p = payload(day=3, gold=400, station_health=900)
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        jobs = [j for j in m.jobs.values() if j.get('item', '').startswith('Station')]
        self.assertTrue(jobs, m.jobs)
        self.assertEqual(jobs[0]['item'], 'StationUpgradeVoucher1')

    def test_no_station_purchase_before_day3(self):
        p = payload(day=2, gold=400, station_health=900)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        jobs = [j for j in m.jobs.values() if j.get('item', '').startswith('Station')]
        self.assertFalse(jobs, m.jobs)

    def test_damage_flag_latches_from_observation(self):
        p = payload(day=3, gold=200, station_health=900)
        m = Memory()
        decide_response(p, m)
        self.assertTrue(m.station_hit)
        self.assertEqual(m.day_start_gold, 200)

    def test_station_voucher_used_at_night_next_to_base(self):
        p = payload(day=3, gold=0, station_health=900)
        p['roundNo'] = (3 - 1) * 130 + 75            # 夜晚
        p['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 24}    # 基地旁
        p['teamOur']['roles'][1]['backpack'] = ['StationUpgradeVoucher1']
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        command = planner.commands.get('2')
        self.assertIsNotNone(command, planner.commands)
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'StationUpgradeVoucher1')


# ---------------------------------------------------------------- 公共夹具
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


def payload_and_sites(p):
    return p, wall_sites(Turn.load(p))


def task_payload(desc):
    p = fixture()
    p['teamOur']['roles'] = [p['teamOur']['roles'][0], unit(4, 'pioneer', 12, 25)]
    p['mapInfo']['zones'] = [{'pos': {'x': 13, 'y': 26}, 'neutralType': 'challengerTaskPoint1'}]
    p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 13, 'y': 26}, 'isValid': True,
                                    'coldDownRounds': 0, 'timeoutRounds': 60,
                                    'scoreReward': 50, 'goldReward': 30,
                                    'taskType': '自进化类1'}]
    p['phaseTask'] = desc
    return p


if __name__ == '__main__':
    unittest.main()
