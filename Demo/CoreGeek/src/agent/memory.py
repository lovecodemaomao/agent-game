"""Per-game memory. Nothing from the judge or LLM is executed in this process."""
from dataclasses import dataclass, field

WALL_PRESSURE_DAY = 3          # 需求6: 第3天白天起开始计算围墙承伤
WALL_PRESSURE_RATIO = 0.5      # 需求6: 昨夜围墙承伤超过该比例 -> 当天转向围墙升级券


@dataclass
class Memory:
    round_no: int = 0
    day: int = 0
    llm_used: int = 0
    pending_llm: dict | None = None
    news: list = field(default_factory=list)
    news_version: int = 0
    analysed_news: int = -1
    news_attempts: int = 0
    news_advice: dict = field(default_factory=dict)
    treasure: dict | None = None
    treasure_attempts: set = field(default_factory=set)
    treasure_done: bool = False
    task: dict | None = None
    task_choice: dict | None = None
    skills: list = field(default_factory=list)
    jobs: dict = field(default_factory=dict)
    last_commands: dict = field(default_factory=dict)
    last_roles: dict = field(default_factory=dict)
    mine_used: dict = field(default_factory=dict)
    mine_blocked_until: dict = field(default_factory=dict)
    mining_roles: dict = field(default_factory=dict)   # 矿工分工: {'stone': unit_id}
    summon_order_done: bool = False                    # 召唤令是否已购买并使用
    day_wall_upgrades: int = 0                         # 当天已完成的围墙升级数
    day_weapon_upgrades: int = 0                       # 当天已完成的武器升级数
    levels: dict = field(default_factory=dict)         # 建筑等级快照: {unit_id: level}
    station_hit: bool = False                          # 基地是否受过伤(需求3: 触发基地升级券)
    day_start_gold: int = 0                            # 当天开始时的金币(基地券档位判断)
    wall_rebuilds: dict = field(default_factory=dict)  # 需求5: {uid: {pos, stage, unit}} 拆墙重建
    prepositioned: set = field(default_factory=set)    # 需求4: 夜间已去下一天岗位的角色
    wall_hp: dict = field(default_factory=dict)        # 需求6: {uid: (health, max_hp)} 逐回合核对承伤
    night_wall_damage: float = 0.0                     # 需求6: 本夜围墙累计掉血
    night_wall_total: float = 0.0                      # 需求6: 本夜围墙累计最大血量(含被打掉的)
    wall_pressure: float = 0.0                         # 需求6: 昨夜承伤比例
    wall_pressure_high: bool = False                   # 需求6: 昨夜承伤 > 50%
    wall_hp_prev: dict = field(default_factory=dict)   # 需求6: 上一回合 {uid: (health, max)}
    night_wall_max: dict = field(default_factory=dict)  # 需求6: {uid: max_hp} 今夜出现过的墙
    night_wall_worst: float = 0.0                      # 需求6: 今夜单面墙最大掉血比例
    wall_worst: float = 0.0                            # 需求6: 昨夜单面墙最大掉血比例
    day_upgrades: dict = field(default_factory=dict)   # 每日已完成升级: {day: {'wall': n, 'weapon': n}}
    previous_mines: dict = field(default_factory=dict)
    failed_steps: dict = field(default_factory=dict)
    movement: dict = field(default_factory=dict)       # uid -> last requested destination
    movement_history: dict = field(default_factory=dict)
    blocked_goals: dict = field(default_factory=dict)  # (uid, destination) -> retry round
    blocked_deliveries: dict = field(default_factory=dict)
    tower_assignments: dict = field(default_factory=dict)
    construction_jobs: dict = field(default_factory=dict)
    response: dict | None = None
    trace: list = field(default_factory=list)

    def event(self, text):
        self.trace.append(text)
        self.trace[:] = self.trace[-30:]

    def observe(self, turn, payload):
        day = (turn.round_no - 1)//130 + 1
        if self.day != day:
            self.day = day
            self.llm_used = 0
            self.news_attempts = 0
            self.day_wall_upgrades = 0
            self.day_weapon_upgrades = 0
            self.day_start_gold = turn.gold          # 需求3: "当天白天开始时的金币"
            self.prepositioned.clear()               # 需求4: 新的一天重新就位防守
            self.wall_rebuilds.clear()
            # 需求6: 第3天起, 用"前一晚围墙承伤是否超过 50%"决定当天券的取向
            total = sum(self.night_wall_max.values())
            self.wall_pressure = (self.night_wall_damage/total) if total > 0 else 0.0
            self.wall_worst = self.night_wall_worst
            # "围墙承伤超过 50%" 的两种读法都算吃紧:
            #   整体 —— 整条防线一夜掉血超过其总血量的一半;
            #   单墙 —— 有任意一面墙一夜被打掉一半以上的血。
            self.wall_pressure_high = bool(
                day >= WALL_PRESSURE_DAY
                and (self.wall_pressure > WALL_PRESSURE_RATIO
                     or self.wall_worst > WALL_PRESSURE_RATIO))
            self.night_wall_damage = 0.0
            self.night_wall_worst = 0.0
            self.night_wall_max = {}
        # 需求6: 累计夜间围墙承伤(掉血 + 被打掉时的剩余血量); 白天我方 remove/重建不计
        self._track_wall_damage(turn)
        # 基地受伤是永久状态: 只要掉过血就一直记着, 用来触发基地升级券
        station = turn.station()
        if station is not None and station.health > 0:
            from .economy import HP
            level = max(1, min(3, station.level))
            if station.health < HP['station'][level-1]:
                self.station_hit = True
        if any(e.get('errorCode') == 5 for e in payload.get('errors', [])):
            self.llm_used = 3
        roles = {str(r.unit_id): r for r in turn.controllable()}
        for cache in (self.blocked_goals, self.blocked_deliveries):
            for key, until in list(cache.items()):
                if until <= turn.round_no or str(key[0]) not in roles:
                    cache.pop(key, None)
        results = payload.get('lastRoundRoleActionResults') or {}
        mines = {p: k for p, k in turn.zones.items() if k in ('stone','iron','copper')}
        for p in list(self.mine_used):
            if mines.get(p) != self.previous_mines.get(p):
                self.mine_used.pop(p, None)
                self.mine_blocked_until.pop(p, None)
        if self.round_no == turn.round_no-1:
            from .protocol import Pos
            for uid, cmd in self.last_commands.items():
                role = roles.get(uid)
                before = self.last_roles.get(uid)
                if role is None or before is None:
                    continue
                failed = results.get(uid, results.get(int(uid))) is False
                if cmd['action'] != 'move':
                    self.movement_history.pop(role.unit_id, None)
                if cmd['action'] == 'move' and (failed or role.pos == before.pos):
                    target = Pos.load(cmd['targetPos'][0])
                    self.failed_steps[role.unit_id] = (target, turn.round_no+2)
                if cmd['action'] == 'move' and role.unit_id in self.movement:
                    goal = self.movement[role.unit_id]
                    old_goal, trail = self.movement_history.get(role.unit_id, (goal, [before.pos]))
                    trail = (trail if old_goal == goal else [before.pos]) + [role.pos]
                    trail = trail[-5:]
                    self.movement_history[role.unit_id] = (goal, trail)
                    stuck = len(trail) >= 4 and len(set(trail[-4:])) == 1
                    cycling = (len(trail) == 5 and trail[0] == trail[2] == trail[4]
                               and trail[1] == trail[3])
                    if stuck or cycling:
                        self.blocked_goals[(role.unit_id, goal)] = turn.round_no + 8
                        job = self.jobs.get(role.unit_id, {})
                        if job.get('type') == 'upgrade':
                            self.blocked_deliveries[(role.unit_id, job['unit'])] = turn.round_no + 8
                        self.jobs.pop(role.unit_id, None)
                        self.tower_assignments.pop(role.unit_id, None)
                        self.construction_jobs.pop(role.unit_id, None)
                        self.movement_history.pop(role.unit_id, None)
                        self.event(f'role {uid}: no movement progress; choose another approach')
                if cmd['action'] == 'collect':
                    target = Pos.load(cmd['targetPos'][0])
                    kind = self.previous_mines.get(target)
                    gained = kind and role.backpack.count(kind) > before.backpack.count(kind)
                    if gained:
                        self.mine_used[target] = self.mine_used.get(target,0)+1
                        job = self.jobs.get(role.unit_id, {})
                        if job.get('type') == 'mine' and job.get('target') == target:
                            job['left'] = max(0, job['left']-1)
                    elif failed or kind:
                        self.mine_blocked_until[target] = turn.round_no+8
                        self.jobs.pop(role.unit_id, None)
                        self.event(f'mine unavailable at {target}; retry after 8 rounds')
                job = self.jobs.get(role.unit_id)
                if job and failed:
                    job['failures'] = job.get('failures',0)+1
                    if job['failures'] >= 3:
                        if job.get('type') == 'upgrade':
                            self.blocked_deliveries[(role.unit_id, job['unit'])] = turn.round_no + 8
                        self.jobs.pop(role.unit_id, None)
                        self.event(f'worker {uid}: replan after 3 failed actions')
        # 统计"当天完成了多少次围墙/武器升级"（用于每日必完成配额）
        for unit in turn.ours:
            key = 'wall' if unit.kind == 'wall' else ('weapon' if unit.kind in
                   ('gatling','railgun','rocket') else None)
            if key is None:
                continue
            before = self.levels.get(unit.unit_id)
            self.levels[unit.unit_id] = max(1, min(3, unit.level))
            if before is not None and unit.level > before:
                bucket = self.day_upgrades.setdefault(day, {'wall': 0, 'weapon': 0})
                bucket[key] += unit.level - before
                if key == 'wall':
                    self.day_wall_upgrades += unit.level - before
                else:
                    self.day_weapon_upgrades += unit.level - before
        self.previous_mines = mines
        for uid in list(self.jobs):
            if str(uid) not in roles:
                self.jobs.pop(uid,None)
        news = payload.get('worldNews') or {}
        entry = {'day': day, 'officialNews': str(news.get('officialNews','')),
                 'folkLegends': str(news.get('folkLegends',''))}
        if (entry['officialNews'] or entry['folkLegends']) and (not self.news or self.news[-1] != entry):
            self.news.append(entry)
            self.news[:] = self.news[-20:]
            self.news_version += 1
        self.round_no = turn.round_no

    def _track_wall_damage(self, turn):
        """需求6: 只在夜间统计围墙承伤 —— 掉血量 + 被打掉墙的剩余血量。

        分母是"今夜出现过的墙"的最大血量之和(逐回合取最大, 日切时结算),
        白天我方 remove/重建 换的是新单位ID, 不计入承伤。
        """
        from .economy import HP
        seen = {}
        for unit in turn.walls():
            level = max(1, min(3, unit.level))
            seen[unit.unit_id] = (unit.health, HP['wall'][level-1])
        self.wall_hp_prev = self.wall_hp
        self.wall_hp = seen
        if turn.is_day:
            return
        for uid, (health, maximum) in seen.items():
            self.night_wall_max[uid] = max(self.night_wall_max.get(uid, 0), maximum)
            before = self.wall_hp_prev.get(uid)
            if before is not None and health < before[0]:
                lost = before[0]-health
                self.night_wall_damage += lost
                if maximum > 0:
                    self.night_wall_worst = max(self.night_wall_worst, lost/maximum)
        for uid, (health, _maximum) in self.wall_hp_prev.items():
            if uid not in seen:
                # 夜里消失 = 被机器人打掉: 剩余血量计入承伤
                self.night_wall_damage += health

    def remember(self, turn, response):
        self.last_commands = response['roleCommandMap'].copy()
        self.last_roles = {str(r.unit_id): r for r in turn.controllable()}
        self.response = response
