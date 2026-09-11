"""Exact obstacle-aware distances for multi-leg economic journeys."""
from collections import deque
from .grid import neighbours
from .protocol import Pos

INF = 10**6


class Geography:
    def __init__(self, turn):
        self.turn = turn
        blocked = set(turn.zones)
        for unit in turn.ours + turn.enemies:
            if unit.health > 0 and unit.kind not in ('worker','pioneer'):
                blocked.update(turn.footprint(unit))
        blocked.update(r.pos for r in turn.robots if r.health > 0)
        self.free = {Pos(x,y) for x in range(turn.width) for y in range(turn.height)
                     if Pos(x,y) not in blocked}
        self.edges = {p: tuple(q for q in neighbours(p) if q in self.free) for p in self.free}
        self.cache = {}

    def seats(self, target):
        return tuple(q for q in neighbours(target) if q in self.free)

    def field(self, seeds):
        key = tuple(sorted(set(seeds), key=lambda q:(q.x,q.y)))
        if key not in self.cache:
            cost = {q:0 for q in key if q in self.free}
            queue = deque(cost)
            while queue:
                current = queue.popleft()
                for q in self.edges[current]:
                    if q not in cost:
                        cost[q] = cost[current]+1
                        queue.append(q)
            self.cache[key] = cost
        return self.cache[key]

    def to(self, target):
        return self.field(self.seats(target))
