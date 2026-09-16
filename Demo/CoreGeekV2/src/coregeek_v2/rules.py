"""Protocol and basic legality from docs/任务书.md and docs/接口文档.md.

Strategic feasibility (profit, future threats) is deliberately excluded.
"""
from collections import Counter
from .world import Pos
from .navigation import distance

DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS
ACTIONS = frozenset(('move', 'attack', 'sell', 'buy', 'build', 'remove', 'acceptTask',
                     'submitAnswer', 'summonTreasure', 'use', 'drop', 'collect'))
WEAPONS = ('gatling', 'railgun', 'rocket')
ATTACK_RANGES = {'gatling': (3, 5, 7), 'railgun': (6, 8, 10), 'rocket': (10, 15, float('inf'))}


def station_footprint(pos):
    return (pos, Pos(pos.x + 1, pos.y), Pos(pos.x, pos.y - 1), Pos(pos.x + 1, pos.y - 1))


def max_health(role):
    return (1500, 3000, 4500)[role.level - 1] if role.kind == 'station' else (1000, 1500, 2000)[role.level - 1]


def build_ring(world, pos):
    station = world.station()
    return min(distance(pos, p) for p in station_footprint(station.pos)) if station else -1


def command_error(world, role_id, command):
    role = world.role(role_id)
    if role is None or role.kind not in ('worker', 'pioneer'):
        return 'not a live controllable role'
    action = command.get('action')
    if action not in ACTIONS:
        return 'unknown action'
    allowed = {'action', 'controllerId', 'targetPos', 'name', 'num', 'taskAnswer', 'item'}
    if action == 'attack':
        allowed.add('_weapon_id')
    if set(command) - allowed:
        return 'unknown command field'
    if action in ('build', 'remove', 'collect') and role.kind != 'worker':
        return 'worker-only action'
    if action in ('acceptTask', 'submitAnswer', 'summonTreasure') and role.kind != 'pioneer':
        return 'pioneer-only action'
    if action == 'build' and not world.is_day or action == 'attack' and world.is_day:
        return 'wrong phase'
    targets = command.get('targetPos', [])
    if not isinstance(targets, (tuple, list)):
        return 'targetPos must be an array'
    try:
        if any(type(p['x']) is not int or type(p['y']) is not int for p in targets):
            return 'coordinates must be integers'
        positions = tuple(Pos.load(p) for p in targets)
    except (KeyError, TypeError, ValueError):
        return 'invalid target position'
    if any(not (0 <= p.x < world.width and 0 <= p.y < world.height) for p in positions):
        return 'target outside map'
    if action in ('move', 'build', 'remove', 'collect', 'summonTreasure') and len(positions) != 1:
        return 'expected exactly one target'
    if action in ('sell', 'buy', 'build', 'use', 'drop') and (not isinstance(command.get('name'), str) or not command['name']):
        return 'missing name'
    if action in ('sell', 'buy') and (type(command.get('num', 1)) is not int or command.get('num', 1) <= 0):
        return 'invalid quantity'
    if action == 'submitAnswer' and not isinstance(command.get('taskAnswer'), str):
        return 'missing taskAnswer'
    if action in ('move', 'build', 'remove', 'collect', 'summonTreasure'):
        if distance(role.pos, positions[0]) != 1:
            return 'target must be adjacent'
    if action in ('move', 'build') and positions[0] in world.occupied():
        return 'target occupied'
    if action == 'collect' and world.zones.get(positions[0]) not in ('stone', 'iron', 'copper'):
        return 'not a mine'
    if action == 'collect' and role.capacity is not None and len(role.backpack) >= role.capacity:
        return 'backpack full'
    if action == 'build':
        name = command['name']
        if name not in WEAPONS + ('wall',):
            return 'invalid building name'
        if build_ring(world, positions[0]) != (2 if name == 'wall' else 1):
            return 'wrong building ring'
        if name == 'wall' and 'stone' not in role.backpack:
            return 'wall needs stone'
        if name in WEAPONS and (world.gold < 25 or sum(r.kind in WEAPONS and r.health > 0 for r in world.roles) >= 3):
            return 'weapon limit or insufficient gold'
    if action == 'remove' and not any(r.kind == 'wall' and r.pos == positions[0] for r in world.roles):
        return 'not an owned wall'
    if action in ('buy', 'sell'):
        zone = 'weaponShop' if action == 'buy' else 'vendor'
        if not any(kind == zone and distance(role.pos, pos) == 1 for pos, kind in world.zones.items()):
            return 'not adjacent to shop'
    if action == 'sell':
        name = command['name']
        if name not in ('stone', 'iron', 'copper') or role.backpack.count(name) < command.get('num', 1):
            return 'invalid sale inventory'
    if action == 'buy':
        prices = {p['name']: p['price'] for p in world.weapon_shop_prices}
        price = prices.get(command['name'])
        quantity = command.get('num', 1)
        if price is None or price * quantity > world.gold:
            return 'item unavailable or insufficient gold'
        if role.capacity is not None and len(role.backpack) + quantity > role.capacity:
            return 'backpack full'
    if action in ('use', 'drop') and command['name'] not in role.backpack:
        return 'item absent'
    if action == 'use':
        name = command['name']
        if ('Voucher' in name or name in ('WallFixer', 'DizzyWeapon', 'Bomb')) and len(positions) != 1:
            return 'item requires one target'
        if len(positions) > 1:
            return 'too many use targets'
        if 'Voucher' in name or name == 'WallFixer':
            target = next((r for r in world.roles if r.pos == positions[0] and r.health > 0), None)
            if target is None:
                return 'missing item target'
            footprint = (target.pos,)
            if target.kind == 'station':
                footprint += (Pos(target.pos.x + 1, target.pos.y), Pos(target.pos.x, target.pos.y - 1),
                              Pos(target.pos.x + 1, target.pos.y - 1))
            if min(distance(role.pos, pos) for pos in footprint) != 1:
                return 'item target not adjacent'
            if name == 'WallFixer' and target.kind != 'wall':
                return 'WallFixer requires wall'
            if 'Voucher' in name:
                kinds = WEAPONS if name.startswith('Weapon') else ('station',) if name.startswith('Station') else ('wall',)
                if target.kind not in kinds or name[-1:] != str(target.level) or target.level >= 3:
                    return 'voucher does not match target level or kind'
    if action == 'summonTreasure':
        items = command.get('item')
        if not isinstance(items, (list, tuple)) or any(not isinstance(i, str) for i in items):
            return 'missing item array'
        if Counter(items) - Counter(role.backpack):
            return 'missing sacrifice items'
    if action in ('acceptTask', 'submitAnswer'):
        prefix = world.team_key[1] + 'TaskPoint'
        if not any(kind.startswith(prefix) and distance(role.pos, pos) <= 1 for pos, kind in world.zones.items()):
            return 'not at owned task point'
        if action == 'submitAnswer' and not world.payload.get('phaseTask'):
            return 'no active task'
    if action == 'attack':
        try:
            weapon = world.role(int(command.get('_weapon_id', -1)))
        except (TypeError, ValueError):
            return 'invalid weapon ID'
        if weapon is None or weapon.kind not in ('gatling', 'railgun', 'rocket'):
            return 'missing weapon'
        if command.get('controllerId') != str(role.id) or distance(role.pos, weapon.pos) != 1:
            return 'invalid weapon controller'
        if len(positions) != (1 if weapon.kind == 'railgun' else weapon.level):
            return 'wrong attack target count'
        if not 1 <= weapon.level <= 3 or weapon.cooldown > 0:
            return 'weapon not ready'
        attack_range = weapon.attack_range or ATTACK_RANGES[weapon.kind][weapon.level - 1]
        if any(distance(weapon.pos, p) > attack_range for p in positions):
            return 'attack out of range'
        if weapon.kind == 'gatling':
            vectors = [(p.x - weapon.pos.x, p.y - weapon.pos.y) for p in positions]
            if any(ax * bx + ay * by < 0 for ax, ay in vectors for bx, by in vectors):
                return 'gatling targets exceed 90 degree cone'
    return None
