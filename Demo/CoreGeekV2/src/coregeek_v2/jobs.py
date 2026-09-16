"""Persistent business jobs. All protocol commands remain in Executor."""
import json
import logging
from collections import Counter
from math import inf
from math import ceil
from .model import Job, Signal, ActionIntent, MoveIntent, ReservationRequest
from .world import Pos, freeze
from .navigation import Navigation, distance, neighbours
from .rules import WEAPONS, max_health, station_footprint
from .policy import POLICY
from .strategy import day_end, use_now, upgrade_item

LOG = logging.getLogger(__name__)


def action(verb, **params):
    return ActionIntent(verb, freeze(params))


class BusinessJob(Job):
    def __init__(self, proposal):
        super().__init__()
        self.p = proposal
        self.data = proposal.data
        self.nav = Navigation()
        self.stage = 'GO'
        self.failures = 0
        self.spent = 0
        self.pending_cost = 0
        self.failed_moves = {}

    def on_outcome(self, outcome):
        if outcome.kind.endswith('_FAILED') or outcome.kind == 'MOVE_BLOCKED':
            cmd = outcome.evidence.get('command', {})
            if cmd.get('action') == 'move':
                target = Pos.load(cmd['targetPos'][0])
                # A free-looking cell may lose a simultaneous race to another
                # team. Detour briefly instead of repeating the same move.
                self.failed_moves[target] = outcome.round_no + 2 + outcome.role_id % 3
                if len(self.failed_moves) > 32:
                    self.failed_moves.pop(next(iter(self.failed_moves)))
            self.failures += 1
            if self.p.kind in ('Return', 'Defense'):
                return Signal.RETRY  # keep emergency control; never idle on cooldown
            return Signal.FAIL if self.failures >= 4 else Signal.RETRY
        if outcome.kind.endswith('_SUCCESS'):
            self.failures = 0
        if outcome.kind == 'BUY_SUCCESS':
            self.spent += self.pending_cost
            self.pending_cost = 0
        return Signal.CONTINUE

    def cost(self, world, start, end):
        return self.nav.cost(world, start, end, True)

    def stand(self, world, start, target, actual=False):
        costs, _ = self.nav.distances(world, start, not actual)
        exits, _ = self.nav.distances(world, self.data['post'], True, self.data.get('return_blocked', ()))
        candidates = {p for cell in self.interaction_cells(world, target) for p in neighbours(cell)}
        return min((p for p in candidates if p in costs and
                    (p in exits or any(q in exits for q in neighbours(p)))),
                   key=lambda p: (costs[p], p), default=None)

    @staticmethod
    def interaction_cells(world, target):
        building = next((r for r in world.roles if r.pos == target and r.kind == 'station'), None)
        return station_footprint(target) if building else (target,)

    def interact_cost(self, world, start, target):
        stand = self.stand(world, start, target)
        return (self.cost(world, start, stand), stand) if stand is not None else (inf, start)

    def closest(self, world, start, kind):
        return min((p for p,k in world.zones.items() if k == kind),
                   key=lambda p: self.interact_cost(world,start,p)[0], default=None)

    def interact(self, world, role_id, target, verb, **params):
        role = world.role(role_id)
        if (min(distance(role.pos, cell) for cell in self.interaction_cells(world, target)) == 1
                and self.return_cost(world, role.pos) < inf):
            if verb in ('collect', 'build', 'remove', 'use', 'summonTreasure'):
                params['targetPos'] = [target.dump()]
            return action(verb, **params)
        stand = self.stand(world, role.pos, target, actual=True)
        return MoveIntent(stand) if stand is not None and stand != role.pos else None

    def return_cost(self, world, pos):
        return self.nav.cost(world, pos, self.data['post'], True, self.data.get('return_blocked', ()))

    def reservation_request(self, world, role_id, binding, current):
        # Reconcile against current inventory and current prices, including
        # unconfirmed purchases and price changes while walking to the shop.
        bag = Counter(world.role(role_id).backpack)
        needed = None
        if self.p.kind in ('Procure', 'Delivery'):
            needed = {self.data['name']: int(not bag[self.data['name']])}
        elif self.p.kind == 'Stock':
            needed = {self.data['name']: max(0, self.data['quantity'] - bag[self.data['name']])}
        elif self.p.kind == 'Treasure':
            needed = Counter(self.data['items']) - bag
        if needed is not None:
            prices = {p['name']: p['price'] for p in world.weapon_shop_prices}
            return ReservationRequest(sum(prices.get(name, inf) * count
                                          for name, count in needed.items() if count), current.keys)
        return ReservationRequest(max(0, self.p.reservation.gold - self.spent), current.keys)

    def remaining_duration(self, world, role_id, binding, navigation):
        return self.return_cost(world, world.role(role_id).pos)


class BuildJob(BusinessJob):
    def check(self, world, role_id, binding):
        if any(r.pos == binding and r.kind == self.data['name'] and r.health > 0 for r in world.roles):
            return Signal.SUCCESS
        if not world.is_day or self.data['name'] == 'wall' and 'stone' not in world.role(role_id).backpack:
            return Signal.FAIL
        self.stage = 'BUILD'
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        cost, stand = self.interact_cost(world, world.role(role_id).pos, binding)
        return cost + 1 + self.return_cost(world, stand)

    def intent(self, world, role_id, binding):
        role = world.role(role_id)
        # A travelling ally can occupy the site after assignment. Wait for a
        # later snapshot; command ordering cannot make construction safe now.
        if binding in world.occupied():
            return None
        costs, _ = self.nav.distances(world, role.pos)
        safe = [p for p in neighbours(binding) if p in costs and p != binding and
                self.nav.cost(world,p,self.data['post'],True,
                              tuple(self.data.get('return_blocked', ())) + (binding,)) < inf]
        stand = min(safe,key=lambda p:(costs[p],p),default=None)
        if stand is None:
            return None
        if role.pos != stand:
            return MoveIntent(stand)
        for other_id, post in self.data.get('role_posts', ()):
            other = world.role(other_id)
            if other and other_id != role_id:
                before = self.nav.cost(world, other.pos, post, True)
                after = self.nav.cost(world, other.pos, post, True, (binding,))
                if before < inf and after == inf:
                    LOG.info('WAIT_BUILD role=%s target=%s reason=would trap ally %s', role_id, binding, other_id)
                    return None
        return action('build',name=self.data['name'],targetPos=[binding.dump()])


class RebuildJob(BuildJob):
    def check(self, world, role_id, binding):
        wall = next((r for r in world.walls() if r.pos == binding), None)
        if wall and wall.health >= max_health(wall):
            return Signal.SUCCESS
        if not world.is_day or 'stone' not in world.role(role_id).backpack:
            return Signal.FAIL
        self.stage = 'REMOVE' if wall else 'BUILD'
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        return super().remaining_duration(world, role_id, binding, navigation) + 1

    def intent(self, world, role_id, binding):
        removing = any(r.pos == binding for r in world.walls())
        return (self.interact(world, role_id, binding, 'remove') if removing
                else super().intent(world, role_id, binding))


class SellJob(BusinessJob):
    def sellable(self, world, role_id):
        bag = Counter(world.role(role_id).backpack)
        bag['stone'] = max(0, bag['stone'] - self.data.get('keep_stone', 0))
        goods = []
        role = world.role(role_id)
        prices = {p['name']:p['price'] for p in world.vendor_prices}
        for kind, number in bag.items():
            if kind not in ('stone','iron','copper') or number <= 0:
                continue
            if kind in self.data.get('hold_ores', ()) and len(role.backpack) < (role.capacity or 100):
                needed = max(0, self.data.get('gold_target', 0) - world.gold)
                number = min(number, ceil(needed / max(.01, prices.get(kind, 0))))
            if number:
                goods.append((kind, number))
        return goods

    def check(self, world, role_id, binding):
        return Signal.CONTINUE if self.sellable(world, role_id) else Signal.SUCCESS

    def remaining_duration(self, world, role_id, binding, navigation):
        cost, stand = self.interact_cost(world, world.role(role_id).pos, binding)
        return cost + len(self.sellable(world, role_id)) + self.return_cost(world, stand)

    def intent(self, world, role_id, binding):
        goods = self.sellable(world, role_id)
        if goods:
            name, num = goods[0]
            self.stage = 'SELL'
            return self.interact(world, role_id, binding, 'sell', name=name, num=num)


class MineTripJob(SellJob):
    def __init__(self, proposal):
        super().__init__(proposal)
        self.stage = 'COLLECT'
        self.collected = 0

    def on_outcome(self, outcome):
        if outcome.kind == 'COLLECT_SUCCESS':
            self.collected += 1
        return super().on_outcome(outcome)

    def check(self, world, role_id, binding):
        role = world.role(role_id)
        if self.stage == 'GO':
            self.stage = 'COLLECT'
        if self.stage == 'COLLECT' and (self.collected >= self.data['quantity'] or
                world.zones.get(binding) != self.data['ore'] or len(role.backpack) >= (role.capacity or 100)):
            needs_cash = world.gold < self.data['gold_target'] or len(role.backpack) + 10 > (role.capacity or 100)
            self.stage = 'SELL' if not self.data['stone_trip'] and needs_cash else 'DONE'
        if self.stage == 'SELL' and not self.sellable(world, role_id):
            self.stage = 'DONE'
        return Signal.SUCCESS if self.stage == 'DONE' else Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        if self.stage == 'DONE':
            return 0
        role = world.role(role_id)
        if self.stage == 'SELL':
            vendor = self.data.get('vendor')
            if vendor is None:
                return inf
            return super().remaining_duration(world, role_id, vendor, navigation)
        cost, stand = self.interact_cost(world, role.pos, binding)
        return cost + max(0, self.data['quantity'] - self.collected) + self.data['tail'] + POLICY.safety_margin

    def intent(self, world, role_id, binding):
        if self.stage == 'SELL':
            return super().intent(world, role_id, self.data['vendor'])
        return self.interact(world, role_id, binding, 'collect')


class ProcurementJob(BusinessJob):
    def check(self, world, role_id, binding):
        role = world.role(role_id)
        target = world.role(self.data['target_id'])
        if not target:
            return Signal.FAIL
        if self.data['name'] == 'WallFixer' and target.health == max_health(target):
            return Signal.SUCCESS
        if 'Voucher' in self.data['name'] and str(target.level) != self.data['name'][-1:]:
            return Signal.SUCCESS
        if self.data['name'] not in role.backpack:
            if self.p.kind == 'Delivery':
                return Signal.FAIL
            if role.capacity is not None and len(role.backpack) >= role.capacity:
                return Signal.FAIL
            self.stage = 'BUY'
        elif not use_now(world, target):
            self.stage = 'HOLD'
            LOG.info('HOLD_VOUCHER role=%s target=%s item=%s', role_id, target.id, self.data['name'])
            return Signal.SUCCESS
        else:
            self.stage = 'USE'
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        role = world.role(role_id)
        pos, cost = role.pos, 0
        if self.data['name'] not in role.backpack:
            shop = self.closest(world, pos, 'weaponShop')
            if shop is None:
                return inf
            leg, pos = self.interact_cost(world, pos, shop)
            cost += leg + 1
        if not self.data.get('hold'):
            leg, pos = self.interact_cost(world, pos, binding)
            cost += leg + 1
        return cost + self.return_cost(world, pos) + POLICY.safety_margin

    def intent(self, world, role_id, binding):
        if self.data['name'] not in world.role(role_id).backpack:
            shop = self.closest(world, world.role(role_id).pos, 'weaponShop')
            self.pending_cost = next((p['price'] for p in world.weapon_shop_prices if p['name'] == self.data['name']), 0)
            self.stage = 'BUY'
            return self.interact(world, role_id, shop, 'buy', name=self.data['name']) if shop else None
        self.stage = 'USE'
        return self.interact(world, role_id, binding, 'use', name=self.data['name'])


class StandbyJob(BusinessJob):
    def check(self, world, role_id, binding):
        # Waiting has no business commitment and releases the role every turn.
        return Signal.SUCCESS

    def remaining_duration(self, world, role_id, binding, navigation):
        cost, stand = self.interact_cost(world, world.role(role_id).pos, binding)
        return cost + self.return_cost(world, stand) + POLICY.safety_margin

    def intent(self, world, role_id, binding):
        stand = self.stand(world, world.role(role_id).pos, binding, actual=True)
        return MoveIntent(stand) if stand is not None and stand != world.role(role_id).pos else None


class StockJob(BusinessJob):
    def check(self, world, role_id, binding):
        role = world.role(role_id)
        missing = max(0, self.data['quantity'] - role.backpack.count(self.data['name']))
        if not missing:
            return Signal.SUCCESS
        if role.capacity is not None and len(role.backpack) + missing > role.capacity:
            return Signal.FAIL
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        cost, stand = self.interact_cost(world, world.role(role_id).pos, binding)
        return cost + 1 + self.return_cost(world,stand) + POLICY.safety_margin

    def intent(self, world, role_id, binding):
        number = max(0, self.data['quantity'] - world.role(role_id).backpack.count(self.data['name']))
        if not number:
            return None
        self.pending_cost = next((p['price']*number for p in world.weapon_shop_prices if p['name']==self.data['name']),0)
        self.stage = 'STOCK'
        return self.interact(world,role_id,binding,'buy',name=self.data['name'],num=number)


class ReturnJob(SellJob):
    def check(self, world, role_id, binding):
        if not world.is_day:
            return Signal.SUCCESS
        self.stage = 'RETURN'
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        # Emergency return is admitted even when already late; best effort must
        # not be rejected for failing a deadline that has become impossible.
        return 0

    def intent(self, world, role_id, binding):
        role = world.role(role_id)
        vendor = self.closest(world, role.pos, 'vendor')
        if vendor and self.sellable(world, role_id):
            cost, stand = self.interact_cost(world, role.pos, vendor)
            if cost + len(self.sellable(world, role_id)) + self.return_cost(world, stand) + 1 <= day_end(world)-world.round_no+1:
                return super().intent(world, role_id, vendor)
        return MoveIntent(self.data['post']) if role.pos != self.data['post'] else None


class DefenseJob(BusinessJob):
    def __init__(self, proposal):
        super().__init__(proposal)
        self.fire_intent = None
        self.maintenance = None

    def check(self, world, role_id, binding):
        return Signal.SUCCESS if world.is_day else Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        return 0

    def combat_candidates(self, world, role_id):
        role = world.role(role_id)
        self.maintenance, self.fire_intent = None, None
        if self.data['mode'] == 'support':
            for building in sorted(world.alive(('station','wall') + WEAPONS), key=lambda r:(r.kind != 'station', r.health/max_health(r))):
                cells = station_footprint(building.pos) if building.kind == 'station' else (building.pos,)
                if min(distance(role.pos,p) for p in cells) != 1:
                    continue
                name = upgrade_item(building) if building.level < 3 else 'WallFixer'
                if name in role.backpack and use_now(world, building) and (name != 'WallFixer' or building.kind == 'wall'):
                    self.maintenance = action('use', name=name, targetPos=[building.pos.dump()])
                    break
                if building.kind == 'wall' and building.health < max_health(building)*POLICY.repair_ratio and 'WallFixer' in role.backpack:
                    self.maintenance = action('use', name='WallFixer', targetPos=[building.pos.dump()])
                    break
            if self.maintenance is None:
                for building in sorted(world.alive(('station','wall')), key=lambda r:(r.kind != 'station', r.health/max_health(r))):
                    if building.health >= max_health(building)*POLICY.emergency_ratio:
                        continue
                    item = upgrade_item(building) if building.level < 3 else 'WallFixer'
                    if item not in role.backpack and not (building.kind=='wall' and 'WallFixer' in role.backpack):
                        continue
                    stand = self.stand(world, role.pos, building.pos, actual=True)
                    if stand is not None and stand != role.pos:
                        self.maintenance = MoveIntent(stand)
                        LOG.info('EMERGENCY_SUPPORT role=%s target=%s hp=%s',role_id,building.id,building.health)
                        break
            if self.maintenance is None:
                close = [r for r in world.robots if distance(r.pos, role.pos) <= 4]
                for name in ('Bomb', 'DizzyWeapon'):
                    if name in role.backpack and len(close) >= 3:
                        center = max((r.pos for r in close), key=lambda p:sum(distance(p,r.pos)<=1 for r in close))
                        self.maintenance = action('use', name=name, targetPos=[center.dump()])
                        break
        if self.maintenance:
            return ()
        kinds = ('rocket',) if self.data['mode']=='rocket' else ('railgun',) if self.data['mode']=='railgun' else WEAPONS
        return tuple(r for r in world.weapons() if r.kind in kinds and r.cooldown == 0 and distance(role.pos,r.pos)==1)

    def intent(self, world, role_id, binding):
        self.stage = 'DEFEND'
        if self.maintenance:
            return self.maintenance
        if self.fire_intent:
            return self.fire_intent
        role = world.role(role_id)
        if role.pos != self.data['post']:
            return MoveIntent(self.data['post'])
        return None


class TreasureJob(BusinessJob):
    def __init__(self, proposal):
        super().__init__(proposal)
        self.attempted = False

    def on_outcome(self, outcome):
        if outcome.kind.startswith('TREASURE_'):
            self.attempted = True
            return Signal.SUCCESS if outcome.kind in ('TREASURE_SUCCESS','TREASURE_EMPTY') else Signal.FAIL
        return super().on_outcome(outcome)

    def check(self, world, role_id, binding):
        if self.attempted or world.round_no > self.data['end']:
            return Signal.FAIL
        role = world.role(role_id)
        missing = Counter(self.data['items']) - Counter(role.backpack)
        if role.capacity is not None and len(role.backpack) + sum(missing.values()) > role.capacity:
            return Signal.FAIL
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        role = world.role(role_id)
        missing = Counter(self.data['items']) - Counter(role.backpack)
        pos, total = role.pos, 0
        if missing:
            shop = self.closest(world, pos, 'weaponShop')
            if shop is None:
                return inf
            leg, pos = self.interact_cost(world, pos, shop)
            total += leg + len(missing)
        leg, pos = self.interact_cost(world, pos, binding)
        total += leg
        total = max(total, self.data['start'] - world.round_no)
        return total + 1 + self.return_cost(world,pos) + POLICY.safety_margin

    def intent(self, world, role_id, binding):
        role = world.role(role_id)
        missing = Counter(self.data['items']) - Counter(role.backpack)
        if missing:
            name, number = next(iter(missing.items()))
            shop = self.closest(world, role.pos, 'weaponShop')
            self.pending_cost = next((p['price']*number for p in world.weapon_shop_prices if p['name']==name), 0)
            self.stage = 'BUY'
            return self.interact(world, role_id, shop, 'buy', name=name, num=number) if shop else None
        self.stage = 'SUMMON'
        if distance(role.pos,binding)==1 and world.round_no < self.data['start']:
            return None
        return self.interact(world, role_id, binding, 'summonTreasure', item=list(self.data['items']))


FACTORIES = {'Build':BuildJob, 'Rebuild':RebuildJob, 'Mine':MineTripJob, 'Sell':SellJob,
             'Procure':ProcurementJob, 'Delivery':ProcurementJob, 'Standby':StandbyJob,
             'Return':ReturnJob, 'Defense':DefenseJob, 'Treasure':TreasureJob, 'Stock':StockJob}
