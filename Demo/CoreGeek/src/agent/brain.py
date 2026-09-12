"""Basic defense: two rockets, one railgun, forward walls and mining economy.

Generic tasks use the judge LLM/sandbox channel; movement and economy stay local.
"""
from collections import Counter
from itertools import permutations, combinations

from .grid import Routes, neighbours
from .memory import Memory
from .geography import Geography
from .economy import Economy
from .tasks import Tasks
from .protocol import Pos, Turn, distance, station_footprint, move_command

LOADOUT = ("rocket", "rocket", "railgun")
RETURN_MARGIN = 5


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


def wall_sites(turn):
    station = turn.station()
    if station is None:
        return ()
    center_x = station.pos.x + 0.5
    sign = 1 if center_x < turn.width / 2 else -1
    # Forward half of the outer ring. The rear stays open for economic trips.
    return tuple(sorted((p for p in ring(turn, 2) if sign*(p.x-center_x) > 0),
                        key=lambda p: (-sign*(p.x-center_x), abs(p.y-(station.pos.y-0.5)), p.y)))


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
        self.prices = {x['name']: float(x['price']) for x in payload.get('vendorShopList', [])}
        self.shop_prices = {x['name']: int(x['price']) for x in payload.get('weaponShopList', [])}
        self.walls = wall_sites(turn)
        self.home_cost_cache = {}
        self.remaining = 70 - (turn.round_no - 1) % 130
        self.economic = Economy(self)

    def route(self, role):
        forbidden = set(self.reserved)
        failed = self.memory.failed_steps.get(role.unit_id)
        if failed and failed[1] >= self.turn.round_no:
            forbidden.add(failed[0])
        return Routes(self.turn, role, forbidden)

    def move(self, role, routes, stand):
        if stand is None:
            return False
        if stand == role.pos:
            return True
        step = routes.first.get(stand)
        if step is None:
            return False
        self.commands[str(role.unit_id)] = move_command(step)
        self.reserved.add(step)
        return True

    def interact(self, role, routes, target, action, **fields):
        stand = routes.adjacent(target)
        if stand is None:
            return False
        if stand == role.pos:
            self.commands[str(role.unit_id)] = {'action': action, **fields}
        else:
            self.move(role, routes, stand)
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
        Tasks(self).run()
        self.economic.prepare()
        if not self.turn.is_day:
            self.night()
            return self.commands
        workers = self.turn.workers()
        maintenance = next((r.unit_id for r in workers
                            if self.memory.jobs.get(r.unit_id,{}).get('type')!='upgrade'),None)
        for role in workers:
            routes = self.route(role)
            # Using a voucher in place takes one turn and must not be suppressed
            # by the generic five-turn return margin.
            if self.economic.act_urgent(role,routes):
                continue
            if self.remaining <= self.home_cost(role, role.pos) + RETURN_MARGIN:
                # 需求3: 回防途中在家附近顺手采矿（不耽误入夜前回位）
                self.economic.harvest_near_home(role, routes)
                continue
            if self.build_tower(role, routes):
                continue
            if (self.memory.day==1 or role.unit_id==maintenance) and self.build_wall(role,routes):
                continue
            self.economic.act(role,routes)
        self.assign_towers(day=True)
        return self.commands

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
        candidates.sort(key=lambda p: (routes.distance(p), p.x, p.y))
        for target in candidates:
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
                if routes.distance(target) == 0:
                    self.gold -= 25
                    self.built.append(kind)
                    self.reserved.add(target)
                return True
        return False

    def missing_walls(self):
        occupied = self.turn.occupied_cells() | self.reserved | self.build_targets
        return [p for p in self.walls if p not in occupied]

    def build_wall(self, role, routes):
        missing = self.missing_walls()
        if not missing:
            return False
        stock = role.backpack.count('stone')
        batch = min(3, (len(missing)+1)//2)
        # 采石分工: 只有被指派采石的工人去攒石头，另一名工人留给经济模块采矿石，
        # 避免"两人都去采石头"导致矿石收入为零（需求2）。
        stone_fetcher = self.economic.family(role) == 'stone'
        nearby = [p for p, kind in self.turn.zones.items()
                  if kind == 'stone' and distance(role.pos, p) <= 1
                  and not self.economic.blocked(p,kind)]
        if stone_fetcher and nearby and stock < batch and not role.backpack_full:
            target = nearby[0]
            if self.enough_time(role, routes, target, batch-stock):
                return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
        if not stock and stone_fetcher:
            # Once established, reserve only a small stone batch per trip.
            mines = [p for p, kind in self.turn.zones.items() if kind == 'stone' and not self.economic.blocked(p,kind)]
            for target in sorted(mines, key=routes.distance):
                if not role.backpack_full and self.enough_time(role, routes, target, 3):
                    return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
            return False
        if not stock:
            return False        # 非采石工且手上无石头 -> 交给经济模块去采矿石
        for target in sorted(missing, key=lambda p: (self.walls.index(p)//4, routes.distance(p))):
            if self.enough_time(role, routes, target) and self.interact(
                    role, routes, target, 'build', name='wall', targetPos=[target.dump()]):
                self.build_targets.add(target)
                if routes.distance(target) == 0:
                    self.reserved.add(target)
                return True
        return False

    def upgrade_options(self):
        return self.economic.options()

    def deliver_upgrade(self,role,routes):
        self.economic.prepare()
        return self.economic.upgrade(role,routes)

    def economy(self,role,routes):
        self.economic.prepare()
        return self.economic.act(role,routes)

    def assign_towers(self, day=False):
        roles = [r for r in self.turn.controllable() if str(r.unit_id) not in self.commands and r.unit_id not in self.engaged]
        towers = list(self.turn.weapons())
        if not roles or not towers:
            return []
        paths = {r.unit_id: self.route(r) for r in roles}
        count = min(len(roles), len(towers))
        best, best_cost = [], float('inf')
        for chosen in combinations(roles, count):
            for fleet in permutations(towers, count):
                cost = sum(paths[r.unit_id].distance(t.pos) for r, t in zip(chosen, fleet))
                # When undermanned, prefer rockets at equal walking cost.
                cost += sum(t.kind != 'rocket' for t in fleet) * 0.1
                if cost < best_cost:
                    best_cost, best = cost, list(zip(chosen, fleet))
        ready = []
        for role, tower in best:
            routes = self.route(role)
            if distance(role.pos, tower.pos) <= 1:
                ready.append((role, tower))
            else:
                self.move(role, routes, routes.adjacent(tower.pos))
        return ready

    def night(self):
        # issue #3: 夜间同样每回合检查围墙血量，低于50%且手上持有 WallFixer
        # 立即修复（只做"已经在墙边"的即时修复，不为修墙长途走动而放弃操炮）。
        self.economic.prepare()
        for role in self.turn.workers():
            if self.economic.upgrade(role, self.route(role)):
                self.engaged.add(role.unit_id)
        ready = self.assign_towers()
        remaining = {r.robot_id: r.health for r in self.turn.robots if r.health > 0}
        for role, tower in sorted(ready, key=lambda pair: pair[1].kind != 'rocket'):
            if tower.cooldown > 0:
                if self.economic.upgrade(role,self.route(role)):
                    continue
                if 'Medicine' in role.backpack and role.health < 100:
                    self.commands[str(role.unit_id)] = {'action': 'use', 'name': 'Medicine'}
                continue
            targets = select_targets(self.turn, tower, remaining)
            if targets:
                self.commands[str(tower.unit_id)] = {'action': 'attack', 'controllerId': str(role.unit_id),
                                                     'targetPos': [p.dump() for p in targets]}


def threat(turn, robot):
    station = turn.station()
    d = min(distance(robot.pos, p) for p in station_footprint(station.pos))
    own = 1.0 if not robot.target_team or robot.target_team == turn.team_type else 0.2
    power = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}.get(robot.kind, 5)
    return own * (1 + power / 20 + 6 / max(1, d-2))


def on_segment(start, end, point):
    # Segment against the robot's closed unit square (center coordinates).
    # Exact edge/corner treatment needs confirmation against the judge.
    low, high = 0.0, 1.0
    for a, b, c in ((start.x, end.x, point.x), (start.y, end.y, point.y)):
        delta = b-a
        if delta == 0:
            if abs(a-c) > 0.5:
                return None
        else:
            left, right = sorted(((c-0.5-a)/delta, (c+0.5-a)/delta))
            low, high = max(low, left), min(high, right)
            if low > high:
                return None
    return low


def shot_damage(turn, tower, target, remaining):
    robots = [r for r in turn.robots if remaining.get(r.robot_id, 0) > 0]
    if tower.kind == 'rocket':
        return {r.robot_id: min(remaining[r.robot_id], 20 if r.pos == target else 10)
                for r in robots if distance(r.pos, target) <= 1}
    hits = []
    for robot in robots:
        entry = on_segment(tower.pos, target, robot.pos)
        if entry is not None:
            hits.append((entry, robot.robot_id))
    hits.sort()
    energy = 10*max(1, tower.level) if tower.kind == 'railgun' else 10
    damage = {}
    for _, rid in hits:
        dealt = min(remaining[rid], energy)
        damage[rid] = dealt
        energy -= dealt
        if tower.kind != 'railgun' or energy <= 0:
            break
    return damage


def select_targets(turn, tower, remaining):
    robots = [r for r in turn.robots if remaining.get(r.robot_id, 0) > 0]
    points = set()
    for robot in robots:
        points.add(robot.pos)
        if tower.kind == 'rocket':
            points.update(neighbours(robot.pos))
    points = sorted((p for p in points if 0 <= p.x < turn.width and 0 <= p.y < turn.height
                     and distance(tower.pos, p) <= tower.range_of_attack()), key=lambda p: (p.x, p.y))
    count = max(1, min(3, tower.level)) if tower.kind in ('rocket', 'gatling') else 1
    weights = {r.robot_id: threat(turn, r) for r in robots}
    selected = []
    for _ in range(count):
        best, best_damage, best_score = None, {}, 0
        for target in points:
            if tower.kind == 'gatling' and any(
                    (target.x-tower.pos.x)*(p.x-tower.pos.x) +
                    (target.y-tower.pos.y)*(p.y-tower.pos.y) < 0 for p in selected):
                continue
            damage = shot_damage(turn, tower, target, remaining)
            score = sum(value*weights[rid] + (4*weights[rid] if value == remaining[rid] else 0)
                        for rid, value in damage.items())
            if score > best_score:
                best, best_damage, best_score = target, damage, score
        if best is None:
            break
        selected.append(best)
        for rid, value in best_damage.items():
            remaining[rid] -= value
    # Interface requires the target count to match the weapon level.
    if selected:
        selected.extend([selected[-1]] * (count-len(selected)))
    return selected
