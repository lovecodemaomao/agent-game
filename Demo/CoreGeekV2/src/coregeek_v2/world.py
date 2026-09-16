"""Immutable server truth. No strategy or predicted mutations live here."""
from collections.abc import Mapping
from dataclasses import dataclass


class FrozenMap(Mapping):
    def __init__(self, values=()):
        object.__setattr__(self, '_items', tuple((k, freeze(v)) for k, v in dict(values).items()))

    def __setattr__(self, name, value):
        raise TypeError('immutable mapping')

    def __getitem__(self, key):
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self):
        return (k for k, _ in self._items)

    def __len__(self):
        return len(self._items)

    def __repr__(self):
        return f'FrozenMap({dict(self)!r})'

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap((k, freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(v) for v in value)
    return value


@dataclass(frozen=True, order=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw):
        return cls(int(raw['x']), int(raw['y']))

    def dump(self):
        return {'x': self.x, 'y': self.y}


@dataclass(frozen=True)
class Role:
    id: int
    kind: str
    pos: Pos
    health: int
    level: int
    backpack: tuple
    cooldown: int = 0
    attack_range: int = 0
    capacity: int | None = None
    target_team: str = ''
    abnormal_state: str = ''

    @property
    def unit_id(self):
        return self.id

    @property
    def robot_id(self):
        return self.id

    def range_of_attack(self):
        from .rules import ATTACK_RANGES
        return self.attack_range or ATTACK_RANGES.get(self.kind, (0, 0, 0))[min(3, max(1, self.level)) - 1]


@dataclass(frozen=True)
class WorldState:
    round_no: int
    team_key: tuple
    gold: float
    width: int
    height: int
    roles: tuple
    enemies: tuple
    robots: tuple
    zones: FrozenMap
    payload: FrozenMap

    @property
    def day_no(self):
        from .rules import ROUNDS_PER_DAY
        return (self.round_no - 1) // ROUNDS_PER_DAY + 1

    @property
    def phase_round(self):
        from .rules import DAY_ROUNDS, ROUNDS_PER_DAY
        offset = (self.round_no - 1) % ROUNDS_PER_DAY
        return offset + 1 if offset < DAY_ROUNDS else offset - DAY_ROUNDS + 1

    @property
    def is_day(self):
        from .rules import DAY_ROUNDS, ROUNDS_PER_DAY
        return (self.round_no - 1) % ROUNDS_PER_DAY < DAY_ROUNDS

    def role(self, role_id):
        return next((r for r in self.roles if r.id == role_id and r.health > 0), None)

    @property
    def team_type(self):
        return self.team_key[1]

    def station(self):
        return next(iter(self.alive(('station',))), None)

    def alive(self, kinds):
        return tuple(r for r in self.roles if r.health > 0 and r.kind in kinds)

    def walls(self):
        return self.alive(('wall',))

    def weapons(self):
        return self.alive(('rocket', 'railgun', 'gatling'))

    @property
    def vendor_prices(self):
        return self.payload.get('vendorShopList', ())

    @property
    def weapon_shop_prices(self):
        return self.payload.get('weaponShopList', ())

    @property
    def phase_task(self):
        return self.payload.get('phaseTask', '')

    def occupied(self):
        cells = set(self.zones)
        for r in self.roles + self.enemies + self.robots:
            if r.health <= 0:
                continue
            cells.add(r.pos)
            if r.kind == 'station':
                cells.update((Pos(r.pos.x + 1, r.pos.y), Pos(r.pos.x, r.pos.y - 1),
                              Pos(r.pos.x + 1, r.pos.y - 1)))
        return frozenset(cells)


class WorldParser:
    def parse(self, payload):
        def roles(team):
            return tuple(Role(int(r['id']), r['roleType'], Pos.load(r['pos']),
                              int(r['health']), int(r.get('level') or 1),
                              tuple(r.get('backpack') or ()), int(r.get('cooldown') or 0),
                              int(r.get('attackRange') or 0),
                              int(r['backPackCapability']) if r.get('backPackCapability') is not None else None,
                              str(r.get('targetTeam', '')), str(r.get('abnormalState', '')))
                         for r in team.get('roles', ()))
        team, info = payload['teamOur'], payload['mapInfo']
        round_no = int(payload['roundNo'])
        if round_no < 1 or int(info['width']) < 1 or int(info['height']) < 1:
            raise ValueError('invalid round or map size')
        ours = roles(team)
        if len({r.id for r in ours}) != len(ours):
            raise ValueError('duplicate role IDs')
        return WorldState(round_no, (str(team['teamId']), str(team['type'])),
                          float(team.get('goldNum') or 0), int(info['width']), int(info['height']),
                          ours, roles(payload.get('teamEnemy') or {}),
                          roles(payload.get('robot') or {}),
                          FrozenMap((Pos.load(z['pos']), z['neutralType']) for z in info.get('zones', ())),
                          freeze(payload))
