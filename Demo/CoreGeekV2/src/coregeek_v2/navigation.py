"""One conservative eight-neighbour navigation implementation."""
from collections import deque
from math import inf
from .world import Pos
from .model import MoveIntent, ActionIntent


def distance(a, b):
    return max(abs(a.x - b.x), abs(a.y - b.y))


def neighbours(pos):
    return tuple(Pos(pos.x + dx, pos.y + dy) for dx, dy in
                 ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)))


class Navigation:
    def __init__(self):
        self._world = None
        self._cache = {}

    def __deepcopy__(self, memo):
        return Navigation()

    def distances(self, world, start, ignore_roles=False, extra_blocked=()):
        if self._world is not world:
            self._world, self._cache = world, {}
        key = (start, ignore_roles, frozenset(extra_blocked))
        if key in self._cache:
            return self._cache[key]
        blocked = set(world.occupied()) - {start}
        if ignore_roles:
            blocked -= {r.pos for r in world.alive(('worker', 'pioneer'))}
        blocked.update(extra_blocked)
        queue, parents, costs = deque([start]), {start: None}, {start: 0}
        while queue:
            here = queue.popleft()
            for nxt in neighbours(here):
                if not (0 <= nxt.x < world.width and 0 <= nxt.y < world.height):
                    continue
                if nxt in blocked or nxt in parents:
                    continue
                parents[nxt], costs[nxt] = here, costs[here] + 1
                queue.append(nxt)
        self._cache[key] = costs, parents
        return costs, parents

    def path(self, world, start, goal, ignore_roles=False, extra_blocked=()):
        if start == goal:
            return (start,)
        costs, parents = self.distances(world, start, ignore_roles, extra_blocked)
        if goal not in costs:
            return None
        route = [goal]
        while parents[route[-1]] is not None:
            route.append(parents[route[-1]])
        return tuple(reversed(route))

    def cost(self, world, start, goal, ignore_roles=False, extra_blocked=()):
        return self.distances(world, start, ignore_roles, extra_blocked)[0].get(goal, inf)

    def adjacent(self, world, start, target, ignore_roles=False):
        costs, _ = self.distances(world, start, ignore_roles)
        return min((p for p in neighbours(target) if p in costs),
                   key=lambda p: (costs[p], p), default=None)

    def itinerary_cost(self, world, points):
        return sum(self.cost(world, a, b) for a, b in zip(points, points[1:]))


class MovementCoordinator:
    def resolve(self, world, intents, navigation):
        result, claimed = [], set()
        # Reserve this turn's construction cells before routing any movement,
        # independently of role order and of the server's execution order.
        building = {Pos.load(p) for _, intent in intents
                    if isinstance(intent, ActionIntent) and intent.action == 'build'
                    for p in intent.params.get('targetPos', ())}
        # Caller supplies scheduler priority order. Never enter an occupied cell,
        # even if its occupant intends to leave (server ordering is not assumed).
        for record, intent in intents:
            if isinstance(intent, MoveIntent):
                failed = {p for p, until in getattr(record.behavior, 'failed_moves', {}).items()
                          if until >= world.round_no}
                route = navigation.path(world, world.role(record.role_id).pos, intent.goal,
                                        extra_blocked=building | claimed | failed)
                if not route or len(route) < 2 or route[1] in claimed:
                    continue
                claimed.add(route[1])
                intent = MoveIntent(route[1])
            result.append((record, intent))
        return result
