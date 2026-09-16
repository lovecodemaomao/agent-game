"""Optional local simulator smoke test. Simulator is NOT a rules authority.

No LLM, external service, or sandbox command is executed. Task solver protocol
is covered separately by deterministic unit tests. Requires simulator deps.
"""
import argparse
from collections import Counter
import json
import logging
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
sys.path.insert(0, str(ROOT / 'src'))
from coregeek_v2.runtime import Agent
from coregeek_v2.world import WorldParser
from coregeek_v2.strategy import post_for
from sim.engine.config import load_config
from sim.engine.provider import EngineProvider


def run(rounds=130, seed=42):
    provider = EngineProvider(load_config(), seed=seed)
    agents = {team: Agent.production(development=True) for team in ('challenger','defender')}
    counts = {team:Counter() for team in agents}
    failures = {team:Counter() for team in agents}
    peaks = {team:0 for team in agents}
    checkpoints = []
    try:
        for turn in range(1, rounds+1):
            responses = {}
            for team, agent in agents.items():
                raw = provider.build_request(team, turn)
                before = time.monotonic()
                response = agent.respond(raw)
                peaks[team] = max(peaks[team], time.monotonic()-before)
                responses[team] = response
                counts[team].update(c['action'] for c in response['roleCommandMap'].values())
                for rid, valid in raw['lastRoundRoleActionResults'].items():
                    if not valid:
                        failures[team][rid] += 1
            for team, response in responses.items():
                provider.consume(team,turn,response,{})
            if turn % 130 in (70,0):
                for team, agent in agents.items():
                    raw = provider.build_request(team, turn)
                    world = WorldParser().parse(raw)
                    plan = next(iter(agent.sessions.values())).runtime.current_day_plan
                    checkpoints.append(dict(round=turn,team=team,gold=world.gold,
                        ready={r.id:r.pos==post_for(world,plan,r) for r in world.alive(('worker','pioneer'))},
                        positions={r.id:r.pos.dump() for r in world.alive(('worker','pioneer'))},
                        posts={r.id:post_for(world,plan,r).dump() for r in world.alive(('worker','pioneer'))},
                        units=[dict(id=r.id,kind=r.kind,pos=r.pos.dump(),health=r.health,level=r.level)
                               for r in world.roles if r.health > 0],
                        weapons=[(r.kind,r.level) for r in world.weapons()],walls=len(world.walls()),
                        base_hp=world.station().health if world.station() else 0))
            if turn % 25 == 0:
                print(f'progress round={turn}', file=sys.stderr, flush=True)
    finally:
        provider.mock_api.stop()
    return dict(seed=seed,rounds=rounds,actions=counts,failed_actions=failures,
                peak_decision_seconds=peaks,checkpoints=checkpoints,
                note='Local simulator only; no live LLM or task sandbox execution.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=130)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output')
    parser.add_argument('--log')
    args = parser.parse_args()
    logging.basicConfig(filename=args.log,level=logging.INFO if args.log else logging.ERROR)
    result = run(args.rounds,args.seed)
    text = json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).write_text(text,encoding='utf-8')
    print(text)
