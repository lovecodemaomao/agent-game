"""Record only emitted commands; correlate server evidence with their origin."""
from dataclasses import dataclass
from .world import Pos, freeze


@dataclass(frozen=True)
class PreviousActionRecord:
    round_no: int
    role_id: int
    job_id: str
    job_stage: str
    command: object


@dataclass(frozen=True)
class Outcome:
    round_no: int
    role_id: int
    origin_job_id: str
    job_stage: str
    kind: str
    evidence: object


class OutcomeBuilder:
    def build(self, previous, actions, world):
        results = []
        for action in actions:
            kind = 'UNCONFIRMED'
            current = world.role(action.role_id)
            before = previous.role(action.role_id) if previous else None
            raw = world.payload
            # Result fields describe the immediately preceding turn only.
            if action.round_no == world.round_no - 1:
                result_id = action.command.get('_weapon_id', action.role_id)
                legal = (raw.get('lastRoundRoleActionResults') or {}).get(str(result_id))
                if legal is None:
                    legal = (raw.get('lastRoundRoleActionResults') or {}).get(result_id)
                cmd = action.command
                name = cmd['action']
                if legal is False:
                    kind = name.upper() + '_FAILED'
                elif name == 'summonTreasure':
                    kind = {1: 'TREASURE_SUCCESS', 2: 'TREASURE_UNAVAILABLE',
                            3: 'TREASURE_WRONG_ITEMS', 4: 'TREASURE_EMPTY'}.get(
                                raw.get('lastSummonTreasureResult'), 'UNCONFIRMED')
                elif name == 'move' and current and before:
                    target = Pos.load(cmd['targetPos'][0])
                    kind = 'MOVE_SUCCESS' if current.pos == target else 'MOVE_BLOCKED'
                elif name == 'collect' and current and before:
                    target = Pos.load(cmd['targetPos'][0])
                    resource = previous.zones.get(target)
                    if resource and current.backpack.count(resource) > before.backpack.count(resource):
                        kind = 'COLLECT_SUCCESS'
                elif name in ('buy', 'sell', 'use', 'drop') and current and before:
                    item = cmd.get('name')
                    delta = current.backpack.count(item) - before.backpack.count(item)
                    if name == 'buy' and delta >= cmd.get('num', 1):
                        kind = 'BUY_SUCCESS'
                    elif name in ('sell', 'drop', 'use') and delta < 0:
                        kind = name.upper() + '_SUCCESS'
                elif name in ('build', 'remove'):
                    target = Pos.load(cmd['targetPos'][0])
                    old = next((r for r in previous.roles if r.pos == target and r.health > 0), None)
                    new = next((r for r in world.roles if r.pos == target and r.health > 0), None)
                    if name == 'build' and new and new.kind == cmd['name'] and (old is None or new.id != old.id):
                        kind = 'BUILD_SUCCESS'
                    elif name == 'remove' and old and old.kind == 'wall' and new is None:
                        kind = 'REMOVE_SUCCESS'
                elif name == 'acceptTask' and world.phase_task and not previous.phase_task:
                    kind = 'ACCEPTTASK_SUCCESS'
                elif name == 'submitAnswer' and previous.phase_task and not world.phase_task and legal is True and not raw.get('errors'):
                    kind = 'SUBMITANSWER_SUCCESS'
            results.append(Outcome(action.round_no, action.role_id, action.job_id,
                                   action.job_stage, kind, freeze({
                                       'command': action.command,
                                       'actionResults': world.payload.get('lastRoundRoleActionResults', {}),
                                       'treasure': world.payload.get('lastSummonTreasureResult'),
                                       'llmResp': world.payload.get('llmResp', ''),
                                       'lastCmdResult': world.payload.get('lastCmdResult', ''),
                                       'errors': world.payload.get('errors', ())})))
        return tuple(results)


class Recorder:
    def record(self, state, world, commands):
        state.previous_actions = tuple(PreviousActionRecord(world.round_no, c.role_id, c.job_id,
                                                            c.stage, freeze(c.command)) for c in commands)
        state.previous_world = world
