"""Per-game memory. Nothing from the judge or LLM is executed in this process."""
from dataclasses import dataclass, field


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
    summon_order_done: bool = False                    # 第一天优先召唤令是否已购买并使用
    previous_mines: dict = field(default_factory=dict)
    failed_steps: dict = field(default_factory=dict)
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
        if any(e.get('errorCode') == 5 for e in payload.get('errors', [])):
            self.llm_used = 3
        roles = {str(r.unit_id): r for r in turn.controllable()}
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
                if cmd['action'] == 'move' and (failed or role.pos == before.pos):
                    target = Pos.load(cmd['targetPos'][0])
                    self.failed_steps[role.unit_id] = (target, turn.round_no+2)
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
                        self.jobs.pop(role.unit_id, None)
                        self.event(f'worker {uid}: replan after 3 failed actions')
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

    def remember(self, turn, response):
        self.last_commands = response['roleCommandMap'].copy()
        self.last_roles = {str(r.unit_id): r for r in turn.controllable()}
        self.response = response
