"""Pure, bounded fleet fire planning against the wave attacking our base.

All ballistic profiles use start-of-round health: predicted kills do not remove
blockers before simultaneous damage resolves. Residual HP only scores useful damage.
"""
from itertools import permutations
from .grid import neighbours
from .protocol import Pos, distance, station_footprint

BEAM_WIDTH = 8


def our_target(turn, robot):
    if robot.target_team in ('challenger', 'defender'):
        return robot.target_team == turn.team_type
    station = turn.station()
    if station is None:
        return False
    enemy = next((u for u in turn.enemies if u.kind == 'station'), None)
    other = enemy.pos if enemy else Pos(turn.width - 2 - station.pos.x, station.pos.y)
    own_distance = min(distance(robot.pos, p) for p in station_footprint(station.pos))
    enemy_distance = min(distance(robot.pos, p) for p in station_footprint(other))
    if own_distance != enemy_distance:
        return own_distance < enemy_distance
    # Ambiguous high/low positions: prefer our half, never guess across mid-map.
    midpoint = (station.pos.x + other.x + 1) / 2
    return robot.pos.x < midpoint if station.pos.x < other.x else robot.pos.x > midpoint


def threat(turn, robot):
    if not our_target(turn, robot):
        return 0.0
    station = turn.station()
    d = min(distance(robot.pos, p) for p in station_footprint(station.pos))
    power = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}.get(robot.kind, 5)
    # Lane alignment is a soft preference; explicit own-wave flankers stay eligible.
    lane = 1 + 0.2 / (1 + abs(robot.pos.y - (station.pos.y - 0.5)))
    return (1 + power / 20 + 6 / max(1, d - 2)) * lane


def on_segment(start, end, point):
    low, high = 0.0, 1.0
    for a, b, c in ((start.x, end.x, point.x), (start.y, end.y, point.y)):
        delta = b - a
        if delta == 0:
            if abs(a - c) > 0.5:
                return None
        else:
            left, right = sorted(((c - 0.5 - a) / delta, (c + 0.5 - a) / delta))
            low, high = max(low, left), min(high, right)
            if low > high:
                return None
    return low


def raw_damage(turn, tower, target):
    robots = [r for r in turn.robots if r.health > 0]
    if tower.kind == 'rocket':
        return {r.robot_id: 20 if r.pos == target else 10
                for r in robots if distance(r.pos, target) <= 1}
    hits = []
    for robot in robots:
        entry = on_segment(tower.pos, target, robot.pos)
        if entry is not None:
            hits.append((entry, robot.robot_id, robot.health))
    energy = 10 * max(1, tower.level) if tower.kind == 'railgun' else 10
    damage = {}
    for _, rid, health in sorted(hits):
        dealt = min(health, energy) if tower.kind == 'railgun' else energy
        damage[rid] = dealt
        energy -= dealt
        if tower.kind != 'railgun' or energy <= 0:
            break
    return damage


def shot_damage(turn, tower, target, remaining):
    return {rid: min(remaining.get(rid, 0), value)
            for rid, value in raw_damage(turn, tower, target).items()}


def candidate_shots(turn, tower, eligible):
    points = set()
    for robot in turn.robots:
        if robot.robot_id not in eligible:
            continue
        points.add(robot.pos)
        if tower.kind == 'rocket':
            points.update(neighbours(robot.pos))
    shots = []
    for point in sorted(points, key=lambda p: (p.x, p.y)):
        if not (0 <= point.x < turn.width and 0 <= point.y < turn.height):
            continue
        if distance(tower.pos, point) > tower.range_of_attack():
            continue
        profile = raw_damage(turn, tower, point)
        if any(profile.get(rid, 0) > 0 for rid in eligible):
            shots.append((point, profile))
    return shots


def apply_damage(remaining, profile, weights):
    result = remaining.copy()
    useful = kills = waste = collateral = score = 0
    for rid, damage in profile.items():
        if rid not in weights:
            collateral += damage
            continue
        dealt = min(result[rid], damage)
        killed = int(result[rid] > 0 and dealt == result[rid])
        result[rid] -= dealt
        useful += dealt
        kills += killed
        waste += damage - dealt
        score += (dealt + 4 * killed) * weights[rid]
    return result, (useful, -collateral, -waste, score, kills)


def volley(tower, shots, initial, weights):
    count = min(3, max(1, tower.level)) if tower.kind in ('rocket', 'gatling') else 1
    # Maximize actual own-wave damage; equal damage prefers no help to the other
    # team, less overkill, then proximity/power/lane threat and completed kills.
    beam = [(initial, (), (0, 0, 0, 0, 0))]
    for _ in range(count):
        children = []
        for remaining, targets, value in beam:
            for point, profile in shots:
                if tower.kind == 'gatling' and any(
                        (point.x-tower.pos.x)*(p.x-tower.pos.x) +
                        (point.y-tower.pos.y)*(p.y-tower.pos.y) < 0 for p in targets):
                    continue
                after, gain = apply_damage(remaining, profile, weights)
                children.append((after, targets + (point,), tuple(a+b for a,b in zip(value,gain))))
        if not children:
            return initial, (), (0, 0, 0, 0, 0)
        children.sort(key=lambda item: item[2], reverse=True)
        # For rockets/railguns future choices depend on damage, not target order.
        # Gatling also retains its cone constraints in the deduplication key.
        beam, seen = [], set()
        for child in children:
            key = (tuple(child[0].items()),
                   frozenset(child[1]) if tower.kind == 'gatling' else None)
            if key in seen:
                continue
            seen.add(key)
            beam.append(child)
            if len(beam) == BEAM_WIDTH:
                break
    return beam[0]


def plan_fire(turn, towers, remaining=None):
    weights = {r.robot_id: threat(turn, r) for r in turn.robots
               if r.health > 0 and our_target(turn, r)}
    initial = {rid: (remaining.get(rid, 0) if remaining is not None else
                    next(r.health for r in turn.robots if r.robot_id == rid)) for rid in weights}
    profiles = {t.unit_id: candidate_shots(turn, t, weights) for t in towers
                if t.cooldown == 0 and t.health > 0}
    available = sorted((t for t in towers if profiles.get(t.unit_id)), key=lambda t:t.unit_id)
    best_plan, best_remaining, best_score = {}, initial, (0, 0, 0, 0, 0, 0)
    # At most three towers: compare all six planning orders, including restricted
    # range weapons first. This is bounded search, not a global optimality claim.
    for order in permutations(available):
        residual, plan, total = initial, {}, (0, 0, 0, 0, 0)
        for tower in order:
            after, targets, value = volley(tower, profiles[tower.unit_id], residual, weights)
            if value[0] <= 0:
                continue  # retain cooldown when another tower already covers all useful damage
            residual = after
            plan[tower.unit_id] = list(targets)
            total = tuple(a+b for a,b in zip(total,value))
        score = total + (-len(plan),)
        if score > best_score:
            best_plan, best_remaining, best_score = plan, residual, score
    return best_plan, best_remaining


def select_targets(turn, tower, remaining):
    plan, after = plan_fire(turn, [tower], remaining)
    remaining.update(after)
    return plan.get(tower.unit_id, [])
