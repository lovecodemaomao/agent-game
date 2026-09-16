"""Task-only probing/LLM loop. Shell text is output to the judge, never run here."""
import json
from .jobs import BusinessJob, action
from .model import Signal
from .policy import POLICY
from .navigation import distance
from .strategy import day_end

# Kept from the proven V1 solver; only the judge sandbox consumes these strings.
TASK_DIR = '/tmp/selfEvolutionTask/'
PROBES = (
    f'find {TASK_DIR} -maxdepth 3 2>/dev/null | head -80',
    f'find {TASK_DIR} -maxdepth 3 -type f -size -64k 2>/dev/null | head -20 | '
    'while read f; do echo "===== $f ====="; cat "$f"; done | head -400',
    f"grep -rInE 'http|api|key|token|param|curl|POST|GET|json' {TASK_DIR} 2>/dev/null | head -60",
)


def parse_json(text):
    text = str(text or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


class TaskJob(BusinessJob):
    def __init__(self, proposal):
        super().__init__(proposal)
        self.description = ''
        self.started = None
        self.pending = None
        self.history = []
        self.probes = 0
        self.answer = None
        self.command = None
        self.submitted = None
        self.reused = False
        self.invalid_responses = 0

    def on_outcome(self, outcome):
        if outcome.kind == 'SUBMITANSWER_SUCCESS':
            self.stage = 'DONE'
            return Signal.SUCCESS
        return super().on_outcome(outcome)

    def check(self, world, role_id, binding):
        if self.stage == 'DONE':
            return Signal.SUCCESS
        role = world.role(role_id)
        if not world.is_day:
            return Signal.FAIL
        if world.phase_task:
            if distance(role.pos, binding) > 1:
                return Signal.FAIL
            if self.description and self.description != world.phase_task:
                return Signal.FAIL
            self.description = world.phase_task
            if self.started is None:
                self.started = world.round_no
            self.stage = 'SOLVE'
        elif self.description:
            return Signal.FAIL  # disappearance alone can be timeout/abandonment
        if self.started and world.round_no >= self.started + self.data['timeout']:
            return Signal.FAIL
        if self.submitted is not None and self.submitted < world.round_no:
            self.history.append({'answer_feedback': world.payload.get('errors', ()), 'task_still_active': bool(world.phase_task)})
            self.answer, self.submitted = None, None
        if self.pending and self.pending['round'] < world.round_no:
            pending, self.pending = self.pending, None
            if pending['round'] != world.round_no - 1:
                self.history.append({'feedback': 'Missing intervening round; retry with current evidence.'})
            elif pending['kind'] == 'command':
                self.history.append({'result': str(world.payload.get('lastCmdResult') or '[missing result]')[:40000]})
            else:
                parsed = parse_json(world.payload.get('llmResp'))
                if parsed and parsed.get('request_id') == pending['request_id']:
                    self.invalid_responses = 0
                    if parsed.get('kind') == 'answer' and 'answer' in parsed:
                        self.answer = parsed['answer'] if isinstance(parsed['answer'],str) else json.dumps(parsed['answer'],ensure_ascii=False)
                    elif parsed.get('kind') == 'command' and isinstance(parsed.get('command'),str) and 0 < len(parsed['command']) <= 16000:
                        self.command = parsed['command']
                else:
                    self.invalid_responses += 1
                    self.history.append({'feedback': 'Missing or mismatched LLM response; do not use it.'})
                    if self.invalid_responses >= 3:
                        return Signal.FAIL
        self.history[:] = self.history[-16:]
        return Signal.CONTINUE

    def remaining_duration(self, world, role_id, binding, navigation):
        cost, stand = self.interact_cost(world, world.role(role_id).pos, binding)
        solve = 1 if world.phase_task else self.data['budget'] + 1
        return cost + solve + self.return_cost(world, stand) + POLICY.safety_margin

    def intent(self, world, role_id, binding):
        if world.phase_task and not self.description:
            self.description, self.started, self.stage = world.phase_task, world.round_no, 'SOLVE'
        if not world.phase_task:
            self.stage = 'ACCEPT'
            return self.interact(world, role_id, binding, 'acceptTask')
        if self.answer is not None:
            self.submitted = world.round_no
            self.history.append({'answer': self.answer})
            return action('submitAnswer', taskAnswer=self.answer)
        return None

    def auxiliary(self, world, role_id, request_id):
        if self.stage != 'SOLVE' or not world.phase_task or self.pending or self.submitted == world.round_no:
            return '', ''
        if not self.reused:
            self.reused = True
            saved = self.data.get('known', {}).get(self.description)
            if saved and saved.get('command'):
                self.command = saved['command']
                self.probes = len(PROBES)
        command, self.command = self.command, None
        if command is None and self.probes < len(PROBES):
            command = PROBES[self.probes]
            self.probes += 1
        if command:
            self.history.append({'command': command})
            self.pending = {'kind':'command', 'round':world.round_no}
            return '', command
        prompt = '''你是比赛开拓者的任务求解器。只返回 JSON：
{"request_id":"原样回传","kind":"command","command":"下一条沙盒命令"}
或 {"request_id":"原样回传","kind":"answer","answer":"题目要求的答案"}。
以任务题干和探测到的文件/接口为准，不臆造地址、参数或结果。探测任务目录为 /tmp/selfEvolutionTask/。
命令只由判题器在沙盒执行（15秒、64KB输出上限），结果下一回合返回。
若接口需要鉴权，仅使用任务文件或接口文档明确提供的方式与凭据，不猜测密钥。
失败时根据真实错误修正，已有足够证据就提交答案；不要重复无效命令。
'''+json.dumps({'request_id':request_id, 'task':self.description,
               'history':self.history, 'errors':world.payload.get('errors',()),
               'remaining_day_rounds':day_end(world)-world.round_no+1},ensure_ascii=False,default=dict)
        self.pending = {'kind':'prompt','round':world.round_no,'request_id':request_id}
        return prompt, ''

    def learning(self):
        command = next((h['command'] for h in reversed(self.history) if 'command' in h), None)
        return self.description, {'command':command, 'answer':self.answer}
