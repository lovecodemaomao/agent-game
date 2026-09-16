"""Daily goals and business proposals. Never writes jobs or commands."""
import logging
from collections import Counter
from dataclasses import replace
from itertools import product
from math import inf, isfinite
from .world import Pos, freeze
from .model import DayPlan, JobProposal, Priority, ReservationRequest
from .navigation import Navigation, distance, neighbours
from .rules import build_ring, max_health, WEAPONS, station_footprint
from .policy import POLICY

LOG = logging.getLogger(__name__)


def day_end(world):
    return (world.day_no - 1) * 130 + 70


def layout(world):
    station = world.station()
    if not station:
        return (), (), freeze({})
    s = station.pos
    enemy = next((r for r in world.enemies if r.kind == 'station'), None)
    fx = 1 if (enemy.pos.x if enemy else world.width / 2) > s.x else -1
    fy = -1 if (enemy.pos.y if enemy else world.height / 2) < s.y else 1
    def transform(dx, dy):
        x = s.x + dx if fx == 1 else s.x + 1 - dx
        y = s.y + dy if fy == -1 else s.y - 1 - dy
        return Pos(x, y)
    desired = [('rocket', transform(2, 1)), ('rocket', transform(2, -1)),
               ('railgun', transform(1, -2))]
    used, weapons = set(), []
    for kind, pos in desired:
        existing = min((r for r in world.weapons() if r.kind == kind and r.id not in used),
                       key=lambda r: distance(r.pos, pos), default=None)
        if existing:
            used.add(existing.id)
            pos = existing.pos
        weapons.append((kind, pos))
    front = tuple(transform(3, y) for y in (-2, -1, 0, 1))
    extensions = tuple(transform(x, y) for x, y in ((3,2),(3,-3),(2,2),(1,2),(2,-3),(1,-3)))
    walls = front if world.day_no == 1 else front + extensions
    static = set(world.occupied()) - {r.pos for r in world.alive(('worker', 'pioneer'))}
    forbidden = static | {p for _, p in weapons} | set(walls)
    rocket_common = set(neighbours(weapons[0][1])) & set(neighbours(weapons[1][1]))
    fallback = transform(2, 0)
    rocket = min((p for p in rocket_common if p not in forbidden),
                 key=lambda p: (distance(p, fallback), p), default=fallback)
    posts = {'rocket': rocket}
    nav = Navigation()
    planned = {p for _, p in weapons} | set(walls)
    def accessible(proposed):
        # A post must remain reachable with ALL other operators already at
        # their posts. Otherwise a support operator can permanently seal the
        # single entrance between the two rocket towers and the station.
        for post in proposed.values():
            blocked = planned | (set(proposed.values()) - {post})
            costs, _ = nav.distances(world, post, True, blocked)
            if not any(min(distance(p, cell) for cell in station_footprint(s)) > 2 for p in costs):
                return False
        return True
    for name, preferred, target in [('railgun', transform(0,-2), weapons[2][1]),
                                     ('support', transform(1,1), s)]:
        cells = set(neighbours(target)) if name != 'support' else {p for tile in station_footprint(s) for p in neighbours(tile)}
        posts[name] = min((p for p in cells if p not in forbidden and p not in posts.values()
                           and 0 <= p.x < world.width and 0 <= p.y < world.height
                           and accessible(dict(posts, **{name:p}))),
                          key=lambda p: (distance(p, preferred), p), default=preferred)
    return tuple(weapons), tuple(p for p in walls if 0 <= p.x < world.width and 0 <= p.y < world.height), freeze(posts)


def post_for(world, plan, role):
    workers = sorted((r for r in world.roles if r.kind == 'worker'), key=lambda r: r.id)
    key = 'support' if role.kind == 'pioneer' else 'rocket' if workers and workers[0].id == role.id else 'railgun'
    return plan.posts.get(key, role.pos)


def mode_for(world, role):
    workers = sorted((r for r in world.roles if r.kind == 'worker'), key=lambda r: r.id)
    return 'support' if role.kind == 'pioneer' else 'rocket' if workers and workers[0].id == role.id else 'railgun'


def key_wall(world, wall, damage):
    station = world.station()
    enemy = next((r for r in world.enemies if r.kind == 'station'), None)
    front = station.pos.x + (3 if (enemy.pos.x if enemy else world.width / 2) > station.pos.x else -2)
    return (wall.pos.x == front and station.pos.y - 2 <= wall.pos.y <= station.pos.y + 1
            and (damage.get(wall.pos, 0) > 0 or wall.health < max_health(wall)))


class DailyStrategy:
    def plan(self, world, memory, previous):
        if previous and previous.day_no == world.day_no:
            return previous
        weapons, walls, posts = layout(world)
        builds = tuple((kind, pos) for kind, pos in weapons if not any(r.pos == pos and r.kind == kind for r in world.weapons()))
        builds += tuple(('wall', pos) for pos in walls if not any(r.pos == pos for r in world.walls()))
        upgrades = tuple(r.id for r in sorted(world.weapons(), key=lambda r: (r.kind != 'rocket', r.level, r.id)) if r.level < 3)
        repairs = tuple(r.id for r in world.walls() if r.health < max_health(r) * POLICY.repair_ratio)
        target = 25 * sum(k != 'wall' for k, _ in builds) + (200 if world.day_no == 1 else 300)
        plan = DayPlan((previous.version + 1) if previous else 1, world.day_no,
                       stone_target=sum(k == 'wall' for k, _ in builds) + POLICY.stone_safety + sum(
                           r.level == 1 and r.health < max_health(r) * POLICY.repair_ratio
                           and not key_wall(world, r, memory.wall_damage) for r in world.walls()), gold_reserve=0,
                       economy_priority=1, task_priority=2, upgrade_policy='rocket_rocket_railgun',
                       required_builds=builds, desired_upgrades=upgrades, required_repairs=repairs,
                       gold_target=target, posts=posts)
        LOG.info('DAILY day=%s builds=%s StoneQuota=%s gold_target=%s posts=%s', world.day_no, builds, plan.stone_target, target, dict(posts))
        return plan


class Context:
    def __init__(self, world, memory, plan, reservations, navigation):
        self.w, self.m, self.plan, self.nav = world, memory, plan, navigation
        self.available = max(0, world.gold - sum(r.gold for r in reservations.values()))
        self.keys = {k for r in reservations.values() for k in r.keys}
        self.workers = sorted(world.alive(('worker',)), key=lambda r: r.id)
        self.pioneer = next(iter(world.alive(('pioneer',))), None)
        self.builds = tuple((k, p) for k, p in plan.required_builds if not any(r.pos == p and r.kind == k and r.health > 0 for r in world.roles))
        self.rebuilds = tuple(r for r in world.walls() if r.level == 1
                             and r.health < max_health(r) * POLICY.repair_ratio
                             and not key_wall(world, r, memory.get('wall_damage', {})))
        self.stone_quota = sum(k == 'wall' for k, _ in self.builds) + len(self.rebuilds) + POLICY.stone_safety
        # Day 2 onward only the engineer builds; stones in Worker A's bag
        # cannot fund Worker B's jobs because the protocol has no transfer.
        builders = self.workers if world.day_no == 1 else self.workers[-1:]
        self.stone = sum(r.backpack.count('stone') for r in builders)
        self.stone_need = max(0, self.stone_quota - self.stone)
        self.prices = {p['name']: float(p['price']) for p in world.vendor_prices}
        self.shop_prices = {p['name']: float(p['price']) for p in world.weapon_shop_prices}

    def post(self, role):
        return post_for(self.w, self.plan, role)

    def keep_stone(self, role):
        return self.stone_quota if self.w.day_no == 1 or self.workers and role.id == self.workers[-1].id else 0

    def return_blocked(self, role):
        return tuple({p for _, p in self.plan.required_builds} |
                     (set(self.plan.posts.values()) - {self.post(role)}))

    def cost(self, start, end):
        return self.nav.cost(self.w, start, end, True)

    def stand(self, start, target):
        return self.nav.adjacent(self.w, start, target, True)

    def near(self, start, kind):
        choices = [(p, self.stand(start, p)) for p, k in self.w.zones.items() if k == kind]
        return min(((p, s) for p, s in choices if s is not None),
                   key=lambda pair: self.cost(start, pair[1]), default=(None, None))

    def tail(self, role, start=None, sell=True):
        pos = start or role.pos
        cost = 0
        if sell and any(i in ('stone', 'iron', 'copper') for i in role.backpack):
            _, stand = self.near(pos, 'vendor')
            if stand:
                cost += self.cost(pos, stand) + len(set(i for i in role.backpack if i in ('stone','iron','copper')))
                pos = stand
        if self.workers and role.id == self.workers[-1].id:
            for kind, target in self.builds + tuple(('rebuild', r.pos) for r in self.rebuilds):
                if kind not in ('wall', 'rebuild'):
                    continue
                stand = self.stand(pos, target)
                if stand:
                    cost += self.cost(pos, stand) + (2 if kind == 'rebuild' else 1)
                    pos = stand
        return cost + self.nav.cost(self.w, pos, self.post(role), True, self.return_blocked(role))

    def proposal(self, kind, key, role, target=None, priority=Priority.NORMAL, utility=0, gold=0, keys=(), **data):
        if self.m.get('failures', {}).get(key, 0) >= self.w.round_no:
            return None
        details = dict(post=self.post(role), return_blocked=self.return_blocked(role),
                       role_posts=tuple((r.id, self.post(r)) for r in self.w.alive(('worker', 'pioneer'))),
                       gold_target=self.plan.gold_target,
                       hold_ores=tuple(e['kind'] for e in self.m.get('advice',{}).get('hold_ores',()) if e['until_day'] > self.w.day_no))
        details.update(data)
        if kind in ('Return', 'Defense'):
            keys += (('night_post', self.post(role)),)
        return JobProposal(kind, key, (role.id,), kind, priority, utility, target,
                           day_end(self.w) if self.w.is_day else self.w.day_no * 130,
                           reservation=ReservationRequest(gold, keys),
                           data=freeze(details))


class Planner:
    def __init__(self, navigation):
        self.nav = navigation

    def propose(self, world, memory, plan, reservations):
        if not world.station():
            return []
        return [p for p in self.candidates(Context(world, memory, plan, reservations, self.nav)) if p is not None]


class ConstructionPlanner(Planner):
    def candidates(self, c):
        if not c.w.is_day:
            return []
        result = []
        for kind, target in c.builds:
            if ('binding', target) in c.keys or target in c.w.occupied():
                continue
            if kind != 'wall' and len(c.w.weapons()) >= 3:
                continue
            workers = c.workers if kind != 'wall' or c.w.day_no == 1 else c.workers[-1:]
            for worker in workers:
                if kind == 'wall' and 'stone' not in worker.backpack:
                    continue
                gold = 0 if kind == 'wall' else 25
                if gold > c.available:
                    continue
                stand = c.stand(worker.pos, target)
                if stand is None:
                    continue
                # Day 1 guns immediately. Walls are fitted into the tail, or
                # built when already nearby without a long economic detour.
                near = c.cost(worker.pos, stand) <= 2
                finishing = day_end(c.w) - c.w.round_no + 1 <= c.tail(worker) + POLICY.safety_margin + 4
                if kind == 'wall' and not (near or finishing):
                    continue
                priority = Priority.CRITICAL_DEFENSE if kind != 'wall' else Priority.NIGHT_PREP if finishing else Priority.NORMAL
                result.append(c.proposal('Build', f'build:{kind}:{target}', worker, target, priority,
                                         1000 - c.cost(worker.pos, stand), gold=gold, name=kind))
        return result


class EconomyPlanner(Planner):
    def candidates(self, c):
        if not c.w.is_day:
            return []
        choices = []
        for role in c.workers:
            options = []
            tail = c.tail(role)
            left = day_end(c.w) - c.w.round_no + 1 - POLICY.safety_margin
            LOG.info('ECON role=%s TailCost=%s budget=%s StoneQuotaRemaining=%s', role.id, tail, left-tail, c.stone_need)
            if left <= tail:
                LOG.info('STOP_MINING role=%s reason=tail deadline', role.id)
                choices.append([None])
                continue
            capacity = role.capacity or 100
            space = capacity - len(role.backpack)
            if space <= 0:
                vendor, stand = c.near(role.pos, 'vendor')
                if stand:
                    options.append(c.proposal('Sell', f'sell:{role.id}', role, vendor, utility=500,
                                              keep_stone=c.keep_stone(role)))
                choices.append(options or [None])
                continue
            engineer = role.id == c.workers[-1].id
            for target, ore in c.w.zones.items():
                if ore not in ('stone', 'iron', 'copper') or ('binding', target) in c.keys:
                    continue
                if any(a.get('kind') == ore and a.get('start_day', 0) <= c.w.day_no <= a.get('end_day', -1)
                       for a in c.m.get('advice', {}).get('blocked_mines', ())):
                    continue
                if engineer and c.stone_need > 0 and ore != 'stone':
                    continue
                stand = c.stand(role.pos, target)
                if stand is None:
                    continue
                travel = c.cost(role.pos, stand)
                remaining = max(1, 10 - c.m.get('mine_used', {}).get(target, 0))
                quantity = min(remaining, space)
                stone_trip = engineer and c.stone_need > 0 and ore == 'stone'
                if stone_trip:
                    quantity = min(quantity, c.stone_need)
                vendor, vendor_stand = c.near(stand, 'vendor')
                sell_cost = 0 if stone_trip else (c.cost(stand, vendor_stand) + 1 if vendor_stand else inf)
                finish = c.tail(role, stand, sell=False) if stone_trip else sell_cost + c.tail(role, vendor_stand, sell=False)
                quantity = min(quantity, int(max(0, left - travel - finish)) if isfinite(finish) else 0)
                if quantity <= 0:
                    continue
                value = quantity * c.prices.get(ore, 0)
                if stone_trip:
                    value = quantity * 15
                extra = max(1, travel + quantity + finish - tail)
                score = value / extra
                if quantity >= remaining:
                    score *= POLICY.completion_bonus if remaining <= 3 else 1.05 if remaining <= 6 else 1
                key = f'mine:{ore}:{target}'
                p = c.proposal('Mine', key, role, target, utility=score, ore=ore, quantity=quantity,
                               stone_trip=stone_trip, vendor=vendor, tail=finish, gold_target=c.plan.gold_target,
                               keep_stone=c.keep_stone(role))
                if p:
                    options.append(p)
            options.sort(key=lambda p: p.utility, reverse=True)
            choices.append(options[:POLICY.mine_candidates] or [None])
        # Pair only proposals, not commands. Scheduler remains the role owner.
        combinations = product(*choices) if choices else []
        best = max((pair for pair in combinations if len([p.target for p in pair if p]) ==
                    len({p.target for p in pair if p})),
                   key=lambda pair: sum(p.utility for p in pair if p), default=())
        if not best:
            best = tuple(opts[0] for opts in choices)
        for p in best:
            if p:
                LOG.info('MINE_CHOICE role=%s target=%s score=%.3f qty=%s', p.eligible_roles, p.target, p.utility, p.data.get('quantity'))
        return list(best)


def upgrade_item(building):
    prefix = 'Station' if building.kind == 'station' else 'Wall' if building.kind == 'wall' else 'Weapon'
    return f'{prefix}UpgradeVoucher{building.level}'


def use_now(world, building):
    if building.kind in WEAPONS:
        return True
    hp_ratio = building.health / max_health(building)
    incoming = sum({'smallRobot':5,'middleRobot':10,'largeRobot':20,'bossRobot':40}.get(r.kind,5)
                   for r in world.robots if distance(r.pos, building.pos) <= 3)
    return hp_ratio <= POLICY.damaged_upgrade_ratio or building.health <= incoming * 3 + 50


class DefensePlanner(Planner):
    def candidates(self, c):
        p = c.pioneer
        result = []
        if not p:
            return result
        buildings = list(c.w.alive(WEAPONS + ('station', 'wall')))
        def rank(r):
            emergency = r.health / max_health(r) < POLICY.emergency_ratio
            if emergency:
                return 0
            if r.kind == 'station' and c.w.day_no == 3 and r.level == 1:
                return 1
            if r.kind in WEAPONS:
                return 2 + int(r.level > 1) + (0 if r.kind == 'rocket' else .1)
            return 4 if r.kind == 'station' else 5
        buildings.sort(key=lambda r: (rank(r), -c.m.get('wall_damage', {}).get(r.pos, 0), r.level, r.id))
        for index, building in enumerate(buildings):
            if building in c.rebuilds:
                continue
            if building.kind == 'station' and c.w.day_no < 3 and building.health > max_health(building) * POLICY.emergency_ratio:
                continue
            emergency = building.health < max_health(building) * POLICY.emergency_ratio
            if building.level < 3:
                name = upgrade_item(building)
            elif building.kind == 'wall' and building.health < max_health(building) * POLICY.repair_ratio:
                name = 'WallFixer'
            else:
                continue
            if building.kind == 'wall' and not c.m.get('wall_damage', {}).get(building.pos) and building.health == max_health(building):
                continue
            if not c.w.is_day:
                # Night repair/use is decided inside the persistent defense Job.
                continue
            held = name in p.backpack
            gold = 0 if held else c.shop_prices.get(name, inf)
            if gold > c.available or (not held and len(p.backpack) >= (p.capacity or 40)):
                continue
            if held and not use_now(c.w, building):
                LOG.info('DEFER_UPGRADE building=%s hp=%s reason=save heal', building.id, building.health)
                continue
            # Keep desired vouchers in inventory; buying does not imply use.
            action = 'Delivery' if held else 'Procure'
            result.append(c.proposal(action, f'{action}:{building.id}:{name}', p, building.pos,
                                     Priority.EMERGENCY if emergency else Priority.NORMAL,
                                     (1000 if held else 100) - index, gold=gold, name=name, target_id=building.id,
                                     hold=not use_now(c.w, building)))
        # Worker B rebuilds ordinary level-1 walls using stone in daytime.
        for wall in c.rebuilds:
            if not c.w.is_day:
                continue
            if any(q and q.target == wall.pos for q in result):
                continue
            for worker in c.workers[-1:]:
                if 'stone' in worker.backpack and c.cost(worker.pos, c.post(worker)) < 8:
                    result.append(c.proposal('Rebuild', f'rebuild:{wall.id}', worker, wall.pos,
                                             Priority.NORMAL, 40, name='wall'))
        if c.w.is_day and c.w.weapons() and all(r.level >= 2 for r in c.w.weapons()):
            count = max(0, 2-p.backpack.count('WallFixer'))
            price = c.shop_prices.get('WallFixer', inf) * count
            shop, _ = c.near(p.pos,'weaponShop')
            if count and price <= c.available and shop and len(p.backpack)+count <= (p.capacity or 40):
                result.append(c.proposal('Stock', f'stock:{p.id}:WallFixer',p,shop,utility=10,
                                         gold=price,name='WallFixer',quantity=2))
        return result


class TaskPlanner(Planner):
    def candidates(self, c):
        p = c.pioneer
        if not p or not c.w.is_day:
            return []
        result = []
        for point in c.w.payload['teamOur'].get('playerTasks', ()):
            target = Pos.load(point['taskPosition'])
            if not c.w.zones.get(target, '').startswith(c.w.team_type + 'TaskPoint'):
                continue
            if not c.w.phase_task and (not point.get('isValid') or point.get('coldDownRounds', 0)):
                continue
            if c.w.phase_task and distance(p.pos, target) > 1:
                continue
            stand = c.stand(p.pos, target)
            if stand is None:
                continue
            cost = c.cost(p.pos, stand)
            budget = min(POLICY.task_budget, int(point.get('timeoutRounds') or POLICY.task_budget))
            if cost + budget + 2 + c.cost(stand, c.post(p)) + POLICY.safety_margin > day_end(c.w) - c.w.round_no + 1:
                continue
            result.append(c.proposal('Task', f'task:{target}', p, target, utility=10000 + float(point.get('goldReward',0)),
                                     budget=budget, timeout=int(point.get('timeoutRounds') or 100),
                                     known=freeze(c.m.get('skills', {}))))
        if c.w.phase_task and not result:
            target = min((q for q,k in c.w.zones.items() if k.startswith(c.w.team_type+'TaskPoint') and distance(p.pos,q)<=1),
                         key=lambda q:q, default=None)
            if target:
                result.append(c.proposal('Task', f'task:{target}', p, target, utility=20000, budget=1, timeout=100, known=freeze(c.m.get('skills', {}))))
        return result


class TreasurePlanner(Planner):
    def candidates(self, c):
        p, treasure = c.pioneer, c.m.get('advice', {}).get('treasure')
        if not p or not c.w.is_day or not treasure or c.w.phase_task:
            return []
        target = Pos.load(treasure['pos'])
        if target in c.m.get('treasure_attempts', ()) or c.w.round_no > treasure['end_round']:
            return []
        missing = Counter(treasure['items']) - Counter(p.backpack)
        cost = sum(c.shop_prices.get(item, inf) * number for item, number in missing.items())
        if cost > c.available or len(p.backpack) + sum(missing.values()) > (p.capacity or 40):
            return []
        return [c.proposal('Treasure', f'treasure:{target}', p, target, utility=200, gold=cost,
                           items=tuple(treasure['items']), start=treasure['start_round'], end=treasure['end_round'])]


class NightPrepPlanner(Planner):
    def candidates(self, c):
        result = []
        for role in c.w.alive(('worker', 'pioneer')):
            post = c.post(role)
            if not c.w.is_day:
                result.append(c.proposal('Defense', f'defense:{c.w.day_no}:{role.id}', role, None,
                                         Priority.NIGHT_PREP, 1000, mode=mode_for(c.w,role)))
                continue
            tail = c.tail(role)
            if day_end(c.w) - c.w.round_no + 1 <= tail + POLICY.safety_margin:
                # Let feasible finishing construction win before plain return.
                result.append(c.proposal('Return', f'return:{c.w.day_no}:{role.id}', role, None,
                                         Priority.NIGHT_PREP, 100, keep_stone=c.keep_stone(role)))
            elif role.kind == 'pioneer':
                shop, stand = c.near(role.pos, 'weaponShop')
                if stand:
                    result.append(c.proposal('Standby', f'standby:{role.id}', role, shop, Priority.OPTIONAL, -1))
        return result
