"""Basic defense: two rockets, one railgun, forward walls and mining economy.

Generic tasks use the judge LLM/sandbox channel; movement and economy stay local.
"""
from collections import Counter
from itertools import permutations, combinations
import math

from .grid import Routes, neighbours
from .memory import Memory
from .geography import Geography, INF
from .economy import Economy
from .tasks import Tasks
from .protocol import (Pos, Turn, build_command, distance, move_command,
                       station_footprint)
from .economy import (DAY1_ORE_PHASE_ROUNDS, DAY_ROUNDS, WALL_MAINTENANCE_DAY)
from .fire_control import plan_fire, select_targets, shot_damage, threat, on_segment

LOADOUT = ("rocket", "rocket", "rocket")   # 要求: 75 金币开局买三座火箭炮
RETURN_MARGIN = 2          # 回到武器旁的少量安全余量
INNER_STAND_SLACK = 8      # 夜间交互站位: 为走到"靠基地内侧"最多多走的步数
RETURN_DEADLINE = 75       # 当天第75回合(含入夜前5回合)前必须回到武器塔旁; 夜间允许移动
PREPOSITION_SAFE_RADIUS = 8   # 夜间预置站位: 目的地该半径内还有机器人就不去
THREAT_RADIUS = 10            # 夜战结束判定: 基地/炮位该半径内还有活机器人就继续防守
                              #   (与火箭炮 1 级射程 10 对齐; 不再用固定夜末回合数等待)


def ring(turn, radius):
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    return tuple(Pos(x, y)
                 for x in range(station.pos.x-radius, station.pos.x+2+radius)
                 for y in range(station.pos.y-1-radius, station.pos.y+1+radius)
                 if min(distance(Pos(x, y), p) for p in footprint) == radius
                 and turn.land(Pos(x, y)))


def wall_sites(turn, extend=0):
    """迎敌半圈围墙格(由前到后排序)。

    extend>0 时沿围墙弧线在两端各再补 extend 格(需求5: 第3天把半圈从 10 格加长到 12 格)。
    """
    station = turn.station()
    if station is None:
        return ()
    center_x = station.pos.x + 0.5
    sign = 1 if center_x < turn.width / 2 else -1
    # Forward half of the outer ring. The rear stays open for economic trips.
    sites = [p for p in ring(turn, 2) if sign*(p.x-center_x) > 0]
    if extend > 0 and sites:
        sites = sites + _arc_extension(turn, station, sites, extend)
    return tuple(sorted(sites, key=lambda p: (-sign*(p.x-center_x),
                                              abs(p.y-(station.pos.y-0.5)), p.y)))


def _arc_extension(turn, station, sites, count):
    """沿环线在围墙弧的两端各补 count 格(按绕基地的角度顺序找相邻的下一格)。"""
    ordered = sorted(ring(turn, 2), key=lambda p: math.atan2(p.y-(station.pos.y-0.5),
                                                              p.x-(station.pos.x+0.5)))
    index = {p: i for i, p in enumerate(ordered)}
    total = len(ordered)
    known = [index[p] for p in sites if p in index]
    if not known or len(known) == total:
        return []
    known_set = set(known)
    start = min(known)
    for _ in range(total):
        if (start-1) % total in known_set:
            start = (start-1) % total
        else:
            break
    end = start
    for _ in range(total):
        if (end+1) % total in known_set:
            end = (end+1) % total
        else:
            break
    extra = [ordered[(start-k) % total] for k in range(1, count+1)]
    extra += [ordered[(end+k) % total] for k in range(1, count+1)]
    return [p for p in extra if turn.land(p)]


def decide_response(payload, memory=None):
    memory = memory if memory is not None else Memory()
    turn = Turn.load(payload)
    memory.observe(turn,payload)
    if turn.station() is None or turn.station().health <= 0:
        response = {'roleCommandMap':{},'prompt':'','executeCmd':''}
    else:
        planner = Planner(turn,payload,memory)
        response = {'roleCommandMap':planner.run(), 'prompt':planner.prompt,
                    'executeCmd':planner.execute_cmd}
    memory.remember(turn,response)
    return response


def decide(payload, memory=None):
    # Compatibility for existing standalone users; HTTP uses the full response.
    return decide_response(payload,memory)['roleCommandMap']


class Planner:
    def __init__(self, turn, payload, memory=None):
        self.turn = turn
        self.memory = memory if memory is not None else Memory(day=(turn.round_no-1)//130+1)
        self.geo = Geography(turn)
        self.engaged = set()
        self.prompt = ''
        self.execute_cmd = ''
        self.payload = payload
        self.commands = {}
        self.reserved = set()
        self.gold = turn.gold
        self.built = []
        self.build_targets = set()
        self.planned_towers = set()
        self.prices = {x['name']: float(x['price']) for x in payload.get('vendorShopList', [])}
        self.shop_prices = {x['name']: int(x['price']) for x in payload.get('weaponShopList', [])}
        # 需求5: 第3天起迎敌半圈围墙两端各加一格(10 -> 12 格)
        self.day = self.memory.day or ((turn.round_no-1)//130+1)
        self.walls = wall_sites(turn, extend=1 if self.day >= WALL_MAINTENANCE_DAY else 0)
        self.home_cost_cache = {}
        self.route_cache = {}
        # 可用回合预算: 白天到当天第 RETURN_DEADLINE 回合为止(含入夜 5 回合)。
        # 夜间在"夜战结束"后可以直接开工(需求4: 夜间可采矿/做任务), 此时把预算
        # 设成"一个完整白天"(RETURN_DEADLINE) —— 与次日白天的预算一致, 这样夜间
        # 选定的矿点/行程到次日清晨不会被重新规划成另一条路线(否则来回摇摆)。
        # 夜战未结束则预算为 0: 只守不干活。
        self.battle_over = self._battle_over(turn)
        if turn.is_day:
            self.remaining = max(0, RETURN_DEADLINE - (turn.round_no - 1) % 130)
        elif self.battle_over:
            self.remaining = RETURN_DEADLINE
        else:
            self.remaining = 0
        self.economic = Economy(self)

    def route(self, role):
        forbidden = set(self.reserved) | self.parking_cells()
        failed = self.memory.failed_steps.get(role.unit_id)
        if failed and failed[1] >= self.turn.round_no:
            forbidden.add(failed[0])
        # A Planner owns one immutable turn. Reservations and failed steps are
        # the only changing route inputs; retain only the latest map per role.
        key = (role.pos, frozenset(forbidden))
        cached = self.route_cache.get(role.unit_id)
        if cached is None or cached[0] != key:
            cached = (key, Routes(self.turn, role, forbidden))
            self.route_cache[role.unit_id] = cached
        return cached[1]

    def parking_cells(self):
        """仍待建造的迎敌半圈围墙格 + 计划建造的炮位: 任何角色都不得停留其上。

        人物一旦站上这些格子, 该格就被占住而建不了墙/炮(需求1: 不能卡在
        面对机器人的那一圈里), 所以把它们从寻路图里排除 —— 既不停留也不穿行,
        路径会自动绕开这一圈, 不必事后纠偏。
        """
        standing = {w.pos for w in self.turn.walls()} | {t.pos for t in self.turn.weapons()}
        return ({p for p in self.walls if p not in standing}
                | {p for p in self.build_targets if p not in standing})

    def move(self, role, routes, stand):
        if stand is None:
            return False
        if self.memory.blocked_goals.get((role.unit_id, stand), 0) > self.turn.round_no:
            return False
        if stand == role.pos:
            return True
        step = routes.first.get(stand)
        if (step is None or step in self.reserved or step in self.build_targets
                or step in self.turn.blocked(role) or not self.turn.land(step)):
            return False
        self.commands[str(role.unit_id)] = move_command(step)
        self.reserved.add(step)
        self.memory.movement[role.unit_id] = stand
        return True

    def inner_stand(self, routes, target, action):
        """夜间交互站位: 只返回严格"靠基地一侧"的相邻格(代价允许时), 否则 None。

        站在围墙外侧会暴露在机器人攻击范围内, 因此夜间对建筑使用券/修复包时,
        要绕到内侧再动手; 内侧不可达(超出允许步数)时返回 None, 由调用方退回
        最短路站位, 保证不会来回打转。
        """
        if self.turn.is_day or action not in ('use', 'build'):
            return None
        station = self.turn.station()
        if station is None:
            return None
        target_d = distance(target, station.pos)
        cands = [q for q in neighbours(target)
                 if q in routes.cost and distance(q, station.pos) < target_d]
        if not cands:
            return None
        nearest = routes.distance(target)
        near = [q for q in cands if routes.cost[q] <= nearest + INNER_STAND_SLACK]
        if not near:
            return None
        return min(near, key=lambda q: (distance(q, station.pos), routes.cost[q], q.x, q.y))

    def interact(self, role, routes, target, action, **fields):
        """与目标交互: 白天走最短路; 夜间优先站到靠基地的内侧再动作。"""
        stand = None
        station = self.turn.station()
        inner = self.inner_stand(routes, target, action)
        if inner is not None and station is not None:
            if routes.distance(target) == 0 and \
                    distance(role.pos, station.pos) <= distance(inner, station.pos):
                stand = role.pos          # 已在可交互位置且不比内侧更外 -> 就地动作
            else:
                stand = inner
        if stand is None:
            stand = routes.adjacent(target)
        previous = self.memory.movement.get(role.unit_id)
        if (stand is not None and stand != role.pos and previous in routes.cost and distance(previous, target) == 1
                and routes.cost[previous] <= routes.cost[stand] + 2
                and self.memory.blocked_goals.get((role.unit_id, previous), 0) <= self.turn.round_no
                and (inner is None or distance(previous, station.pos) <= distance(stand, station.pos))):
            stand = previous
        if (stand is not None and
                self.memory.blocked_goals.get((role.unit_id, stand), 0) > self.turn.round_no):
            candidates = [q for q in neighbours(target) if q in routes.cost
                          and self.memory.blocked_goals.get((role.unit_id, q), 0) <= self.turn.round_no]
            stand = min(candidates, key=lambda q: (routes.cost[q], q.x, q.y), default=None)
        if stand is None:
            return False
        if stand == role.pos:
            self.commands[str(role.unit_id)] = {'action': action, **fields}
        else:
            return self.move(role, routes, stand)
        return True

    def home_cost(self, role, pos):
        if role.unit_id not in self.home_cost_cache:
            from collections import deque
            blocked = self.turn.blocked(role)
            targets = [t.pos for t in self.turn.weapons()] or [self.turn.station().pos]
            seeds = {q for target in targets for q in neighbours(target)
                     if self.turn.land(q) and q not in blocked}
            costs = {q: 0 for q in seeds}
            queue = deque(seeds)
            while queue:
                current = queue.popleft()
                for q in neighbours(current):
                    if q not in costs and q not in blocked and self.turn.land(q):
                        costs[q] = costs[current] + 1
                        queue.append(q)
            self.home_cost_cache[role.unit_id] = costs
        costs = self.home_cost_cache[role.unit_id]
        if pos in costs:
            return costs[pos]
        return min((costs.get(q, 10**6) for q in neighbours(pos)), default=10**6)

    def enough_time(self, role, routes, target, actions=1):
        stand = routes.adjacent(target)
        return stand is not None and (routes.cost[stand] + actions +
                self.home_cost(role, stand) + RETURN_MARGIN < self.remaining)

    def run(self):
        if not self.turn.is_day:
            tasks = Tasks(self)
            if self.battle_over:
                # 需求4: 夜战结束后开拓者立即去任务点继续做任务(领取/执行/提交)
                tasks.run()
            else:
                tasks.sync_task()
                tasks.receive()
            self.night()
            return self.commands
        if self.payload.get('phaseTask'):
            self.engaged.update(r.unit_id for r in self.turn.alive(('pioneer',)))
        self.economic.prepare()
        # Existing inventory belongs to every role, including the pioneer.
        # Active tasks retain their role; consumables never overwrite task commands.
        for role in self.turn.controllable():
            if role.unit_id in self.engaged or str(role.unit_id) in self.commands:
                continue
            if self.economic.use_consumable(role):
                self.engaged.add(role.unit_id)
            elif role.kind == 'pioneer' and self.economic.upgrade(role, self.route(role)):
                self.engaged.add(role.unit_id)
        # Only active tasks hold the pioneer unconditionally. Existing inventory
        # gets a chance before accepting a new task or starting a treasure trip.
        if self.payload.get('phaseTask'):
            self.engaged.difference_update(r.unit_id for r in self.turn.alive(('pioneer',)))
        Tasks(self).run()
        self.pioneer_standby()
        workers = self.turn.workers()
        maintenance = next((r.unit_id for r in workers
                            if self.memory.jobs.get(r.unit_id,{}).get('type')!='upgrade'),None)
        for role in workers:
            if role.unit_id in self.engaged:
                continue
            routes = self.route(role)
            # Using a voucher in place takes one turn and must not be suppressed
            # by the generic five-turn return margin.
            if self.economic.act_urgent(role,routes):
                continue
            # 回家前: 先判断是否该去商店把闲钱花掉（时机提前到"还够走一趟商店"）
            if self.economic.spend_ready(role, routes) and self.economic.spend_before_home(role, routes):
                continue
            if self.remaining <= self.home_cost(role, role.pos) + RETURN_MARGIN:
                # 再顺手采家门口的矿, 最后回位
                self.economic.harvest_near_home(role, routes)
                continue
            if self.build_tower(role, routes):
                continue
            # 半圈围墙未完成 -> 两名工人一起把墙搭好; 搭好后只留维护工补墙
            if (self.missing_walls() or role.unit_id==maintenance) and self.build_wall(role,routes):
                continue
            # 需求5: 第3天起, 掉血的一级墙拆掉重建(石头免费)
            if self.maintain_walls(role, routes):
                continue
            self.economic.act(role,routes)
        self.assign_towers(day=True)
        return self.commands

    def pioneer_standby(self):
        """需求2: 开拓者没有任务可做时去商店旁待命并按计划买券, 不在基地空转。

        任务/寻宝优先级更高(Tasks.run 已先跑过); 只有"确实无事可做"才去商店。
        """
        pioneer = next(iter(self.turn.alive(('pioneer',))), None)
        if pioneer is None or pioneer.unit_id in self.engaged:
            return False
        if str(pioneer.unit_id) in self.commands:
            return False
        if self.memory.task is not None or self.memory.task_choice:
            return False
        if self.economic.shop_standby(pioneer, self.route(pioneer)):
            self.engaged.add(pioneer.unit_id)
            return True
        return False

    def build_tower(self, role, routes):
        towers = self.turn.weapons()
        if len(towers) + len(self.built) >= 3 or self.gold < 25:
            return False
        counts = Counter(t.kind for t in towers) + Counter(self.built)
        kind = next((k for k in LOADOUT if counts[k] < LOADOUT.count(k)), None)
        if kind is None:
            return False
        blocked = self.turn.occupied_cells() | self.reserved | self.build_targets
        candidates = [p for p in ring(self.turn, 1) if p not in blocked]
        previous = self.memory.construction_jobs.get(role.unit_id)
        candidates.sort(key=lambda p: (previous != (kind, p), routes.distance(p), p.x, p.y))
        for target in candidates:
            if not self.construction_accessible(target, tower=True):
                continue
            if not self.enough_time(role, routes, target):
                continue
            # Keep at least three distinct free operating cells for the final fleet.
            fleet = [t.pos for t in towers] + list(self.build_targets) + [target]
            seats = set(q for p in fleet for q in neighbours(p)
                        if self.turn.land(q) and q not in blocked and q not in fleet)
            if len(seats) < 3:
                continue
            if self.interact(role, routes, target, 'build', name=kind, targetPos=[target.dump()]):
                self.build_targets.add(target)
                self.planned_towers.add(target)
                self.memory.construction_jobs[role.unit_id] = (kind, target)
                if routes.distance(target) == 0:
                    self.gold -= 25
                    self.built.append(kind)
                    self.reserved.add(target)
                return True
        return False

    def construction_accessible(self, target, tower=False):
        """Keep workers and distinct operating seats connected to the outside."""
        from collections import deque
        free = self.geo.free - self.build_targets
        outside = set(ring(self.turn, 3)) & free
        def reachable(cells):
            seen = outside & cells
            queue = deque(seen)
            while queue:
                for q in neighbours(queue.popleft()):
                    if q in cells and q not in seen:
                        seen.add(q)
                        queue.append(q)
            return seen
        before = reachable(free)
        after = reachable(free - {target})
        if any(r.pos in before and r.pos not in after for r in self.turn.controllable()):
            return False
        fleet = [t.pos for t in self.turn.weapons()] + list(self.planned_towers)
        if tower:
            fleet.append(target)
        seats = [set(neighbours(p)) & after for p in fleet]
        def assign(index, used):
            return index == len(seats) or any(assign(index+1, used | {q})
                                             for q in seats[index] - used)
        return assign(0, set())

    def missing_walls(self):
        occupied = self.turn.occupied_cells() | self.reserved | self.build_targets
        return [p for p in self.walls if p not in occupied]

    def build_wall(self, role, routes):
        missing = self.missing_walls()
        if not missing:
            return False
        stock = role.backpack.count('stone')
        batch = min(3, (len(missing)+1)//2)
        # 要求3: 第1天前 30 回合先全员采铁/铜赚钱, 之后再采石修墙(不提前耗在采石上)
        if self.memory.day == 1 and (self.turn.round_no - 1) % 130 + 1 <= DAY1_ORE_PHASE_ROUNDS:
            return False
        # 采石分工: 只有被指派采石的工人去攒石头，另一名工人留给经济模块采矿石，
        # 避免"两人都去采石头"导致矿石收入为零（需求2）。
        # 半圈围墙没搭完之前, 两名工人都可以采石建墙（先把防线立起来）;
        # 半圈搭好后不需要石头, 两人一起采矿（分工自然退化为全员采矿）。
        stone_fetcher = (self.economic.family(role) == 'stone'
                         or bool(missing))
        nearby = [p for p, kind in self.turn.zones.items()
                  if kind == 'stone' and distance(role.pos, p) <= 1
                  and not self.economic.blocked(p,kind)
                  and self.economic.mine_claims.get(p, 0) == 0]
        if stone_fetcher and nearby and stock < batch and not role.backpack_full:
            target = nearby[0]
            if self.enough_time(role, routes, target, batch-stock):
                return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
        if not stock and stone_fetcher:
            # Once established, reserve only a small stone batch per trip.
            return self.fetch_stone(role, routes, 3)
        if not stock:
            return False        # 非采石工且手上无石头 -> 交给经济模块去采矿石
        previous = self.memory.construction_jobs.get(role.unit_id)
        for target in sorted(missing, key=lambda p: (previous != ('wall', p),
                                                    self.walls.index(p)//4, routes.distance(p))):
            if not self.construction_accessible(target):
                continue
            if self.enough_time(role, routes, target) and self.interact(
                    role, routes, target, 'build', name='wall', targetPos=[target.dump()]):
                self.build_targets.add(target)
                self.memory.construction_jobs[role.unit_id] = ('wall', target)
                if routes.distance(target) == 0:
                    self.reserved.add(target)
                return True
        return False

    def fetch_stone(self, role, routes, need=1):
        """去最近的石矿采够 need 块石头(认领矿点, 避免两名工人争同一矿)。"""
        if role.backpack_full:
            return False
        mines = [p for p, kind in self.turn.zones.items()
                 if kind == 'stone' and not self.economic.blocked(p, kind)
                 and self.economic.mine_claims.get(p, 0) == 0]
        for target in sorted(mines, key=routes.distance):
            if self.enough_time(role, routes, target, max(1, need)):
                self.economic.mine_claims[target] += 1
                return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
        return False

    def maintain_walls(self, role, routes):
        """需求5: 第3天白天, 掉血的一级墙拆掉重建(拆除不回收石头, 重建即回满血)。

        两回合一步: 先站到墙边 remove, 下一回合在原地 build 一块新墙(消耗背包石头)。
        同时只安排一名工人做重建, 另一名继续挣钱; 掉血的二级墙交给修复包链路。
        """
        if not self.turn.is_day:
            return False
        job = self.memory.wall_rebuilds.get(role.unit_id)
        if job is not None:
            alive = any(w.pos == job['pos'] for w in self.turn.walls())
            stale = job['pos'] not in self.walls or (job['stage'] == 'build' and alive)
            if stale:
                self.memory.wall_rebuilds.pop(role.unit_id, None)
                job = None
        if job is None:
            if self.memory.wall_rebuilds:
                return False        # 已有工人在重建, 不重复派人
            targets = self.economic.rebuild_targets()
            if not targets:
                return False
            best = min(targets, key=lambda w: routes.distance(w.pos))
            if routes.distance(best.pos) >= INF:
                return False
            if not self.enough_time(role, routes, best.pos, 3):    # 拆除 + 重建两回合
                return False
            job = {'pos': best.pos, 'stage': 'remove', 'unit': best.unit_id}
            self.memory.wall_rebuilds[role.unit_id] = job
            self.memory.event(f'worker {role.unit_id}: rebuild damaged level-1 wall {best.unit_id}')
        if job['stage'] == 'remove':
            if distance(role.pos, job['pos']) <= 1:
                self.commands[str(role.unit_id)] = {'action': 'remove',
                                                    'targetPos': [job['pos'].dump()]}
                job['stage'] = 'build'
                self.memory.construction_jobs[role.unit_id] = ('wall', job['pos'])
                return True
            return self.move(role, routes, routes.adjacent(job['pos']))
        if role.backpack.count('stone') < 1:
            return self.fetch_stone(role, routes, 1)      # 先备料再重建
        if distance(role.pos, job['pos']) <= 1:
            self.commands[str(role.unit_id)] = build_command(job['pos'], 'wall')
            self.memory.wall_rebuilds.pop(role.unit_id, None)
            self.build_targets.add(job['pos'])
            return True
        return self.move(role, routes, routes.adjacent(job['pos']))

    def upgrade_options(self):
        return self.economic.options()

    def deliver_upgrade(self,role,routes):
        self.economic.prepare()
        return self.economic.upgrade(role,routes)

    def economy(self,role,routes):
        self.economic.prepare()
        return self.economic.act(role,routes)

    @staticmethod
    def _battle_over(turn):
        """夜战是否已结束: 只要阵前(基地/炮位 THREAT_RADIUS 格内)没有活着的机器人就算结束。

        用"威胁半径"而不是"夜末固定回合"判断 —— 之前写成"等不到某个回合就不动",
        实战里机器人清空后人物还会干等到夜里第 50 多回合才动(需求: 一清场就行动)。
        威胁半径以武器塔为锚点; 还没有武器塔时以基地占地为锚点。
        远处(半径外)仍在游走的机器人不算威胁: 它们一旦靠近, 下一回合判定就会翻回
        "战斗中", 已经离岗的角色会被炮位分配召回(见 night() 的 else 分支)。
        """
        anchors = [t.pos for t in turn.weapons()]
        if not anchors:
            station = turn.station()
            anchors = list(station_footprint(station.pos)) if station is not None else []
        if not anchors:
            return True
        return not any(r.health > 0
                       and min(distance(r.pos, p) for p in anchors) <= THREAT_RADIUS
                       for r in turn.robots)

    def night_battle_over(self):
        return self.battle_over

    def post_target(self, role, routes):
        """下一天的岗位: 开拓者 -> 任务点旁; 工人 -> 次日矿点的采集邻格。"""
        if role.kind == 'pioneer':
            return Tasks(self).next_day_post(role)
        return self.economic.next_day_mine(role, routes)

    def continue_preposition(self, role):
        """已在去岗位路上的角色继续走 —— 它们离开了炮位, 不会出现在 ready 里。"""
        if role.unit_id not in self.memory.prepositioned:
            return False
        routes = self.route(role)
        stand = self.post_target(role, routes)
        if stand is None or stand == role.pos:
            return False
        return self.move(role, routes, stand)

    def preposition(self, role):
        """需求4: 夜战结束后先去占下一天的岗位 —— 工人到次日矿点旁, 开拓者到任务点旁。"""
        if role.unit_id in self.memory.prepositioned:
            return True                      # 已经在去岗位的路上, 不再被炮位分配拉回
        if not self.night_battle_over():
            return False
        station = self.turn.station()
        if station is None:
            return False
        routes = self.route(role)
        stand = self.post_target(role, routes)
        if stand is None or stand == role.pos:
            return False
        if distance(stand, station.pos) <= 2:
            return False                     # 本来就在基地旁, 不必挪
        if any(r.health > 0 and distance(r.pos, stand) <= PREPOSITION_SAFE_RADIUS
               for r in self.turn.robots):
            return False                     # 目的地附近还有机器人 -> 不冒险
        self.memory.prepositioned.add(role.unit_id)
        self.memory.event(f'role {role.unit_id}: night move to next-day post {stand}')
        return self.move(role, routes, stand)

    def assign_towers(self, day=False):
        roles = [r for r in self.turn.controllable() if str(r.unit_id) not in self.commands
                 and r.unit_id not in self.engaged
                 and r.unit_id not in self.memory.prepositioned]
        towers = list(self.turn.weapons())
        if not roles or not towers:
            return []
        paths = {r.unit_id: self.route(r) for r in roles}
        best, best_cost = [], None
        for count in range(min(len(roles), len(towers)), 0, -1):
            for chosen in combinations(roles, count):
                for fleet in permutations(towers, count):
                    distances = [paths[r.unit_id].distance(t.pos) for r,t in zip(chosen,fleet)]
                    if any(d >= 10**6 for d in distances):
                        continue
                    switching = sum(3 for r,t in zip(chosen,fleet)
                                    if self.memory.tower_assignments.get(r.unit_id,t.unit_id) != t.unit_id)
                    cost = (-sum(d == 0 for d in distances), sum(distances)+switching,
                            sum(t.kind != 'rocket' for t in fleet))
                    if best_cost is None or cost < best_cost:
                        best_cost, best = cost, list(zip(chosen, fleet))
            if best:
                break
        ready = []
        for role, tower in best:
            routes = self.route(role)
            self.memory.tower_assignments[role.unit_id] = tower.unit_id
            if distance(role.pos, tower.pos) <= 1:
                ready.append((role, tower))
            else:
                seats = [q for q in neighbours(tower.pos) if q in routes.cost
                         and self.memory.blocked_goals.get((role.unit_id, q), 0) <= self.turn.round_no]
                self.move(role, routes, min(seats, key=lambda q: (routes.cost[q], q.x, q.y), default=None))
        return ready

    def night(self):
        # Defense owns all available operators at night. No delivery trip may
        # remove a controller; cooldown/idle turns may use items in place only.
        self.economic.prepare()
        ready = self.assign_towers()
        plan, _ = plan_fire(self.turn, [tower for _,tower in ready])
        serviced = set()
        for role, tower in ready:
            targets = plan.get(tower.unit_id)
            if targets:
                self.commands[str(tower.unit_id)] = {'action': 'attack', 'controllerId': str(role.unit_id),
                                                     'targetPos': [p.dump() for p in targets]}
            elif not self.economic.use_consumable(role):
                options = self.economic.held_options(role)
                adjacent = [o for o in options if distance(role.pos, o[4].pos) <= 1
                            and o[2] not in serviced and o[2] not in plan]
                if adjacent:
                    _,_,_,item,target = min(adjacent, key=lambda o:(o[0],o[2]))
                    self.commands[str(role.unit_id)] = {'action':'use','name':item,
                                                        'targetPos':[target.pos.dump()]}
                    serviced.add(target.unit_id)
                elif self.preposition(role):
                    continue
        # 需求4: 已经离开炮位去占下一天岗位的角色继续前进
        for role in self.turn.controllable():
            if str(role.unit_id) in self.commands:
                continue
            self.continue_preposition(role)
        # 需求4: 夜战结束后夜间可以采矿/出售 —— 工人立刻在下一天要去的矿点开工
        # (夜间没打完则不进这里: 上面的防守/回塔逻辑已经占了指令)
        if self.battle_over:
            for role in self.turn.workers():
                if str(role.unit_id) in self.commands or role.unit_id in self.engaged:
                    continue
                if self.economic.act_urgent(role, self.route(role)) or \
                        self.economic.act(role, self.route(role)):
                    # 已开工的角色按"已占下一天岗位"记账, 否则下一回合会被
                    # 炮位分配拉回基地, 与研究矿点的行程来回摇摆(实测 2 格死循环)
                    self.memory.prepositioned.add(role.unit_id)
        else:
            self.memory.prepositioned.clear()   # 战斗重新开始 -> 交回炮位分配
