"""Seeded synthetic comparison; run from a Git checkout, not an official match.

python -B Demo/CoreGeek/tests/benchmark_fire_control.py
"""
import ast
import json
from pathlib import Path
import random
import subprocess
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parent)]
from agent.protocol import Pos, Turn, distance, station_footprint
from agent.grid import neighbours
from agent.fire_control import plan_fire, raw_damage, our_target
from test_fire_control import battle, robot
from test_strategy import unit


def evaluate(turn, plan):
    damage = {r.robot_id: 0 for r in turn.robots}
    for tower in turn.weapons():
        for point in plan.get(tower.unit_id, []):
            for rid, value in raw_damage(turn, tower, point).items():
                damage[rid] += value
    return (
        sum(min(r.health, damage[r.robot_id]) for r in turn.robots if our_target(turn, r)),
        sum(min(r.health, damage[r.robot_id]) for r in turn.robots if not our_target(turn, r)),
        sum(max(0, damage[r.robot_id]-r.health) for r in turn.robots),
    )


def main():
    source = subprocess.check_output(['git', 'show', 'e05e827:Demo/CoreGeek/src/agent/brain.py'],
                                     encoding='utf-8', cwd=Path(__file__).resolve().parents[3])
    names = {'threat', 'on_segment', 'shot_damage', 'select_targets'}
    functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = dict(Pos=Pos, distance=distance, station_footprint=station_footprint, neighbours=neighbours)
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<baseline-fire-control>', 'exec'), namespace)
    rng = random.Random(20260913)
    old_total, new_total, times, outcomes = [0]*3, [0]*3, [], [0]*3
    for _ in range(40):
        p = battle()
        p['teamOur']['roles'] += [unit(10+i, 'rocket', 7, 12+i*3, level=rng.randint(1,3)) for i in range(3)]
        positions = rng.sample([(x,y) for x in range(10,20) for y in range(10,21)], rng.randint(2,18))
        p['robot']['roles'] = [robot(100+i,x,y,hp=rng.choice([10,15,20,30,60,100]))
                               for i,(x,y) in enumerate(positions)]
        p['robot']['roles'] += [robot(300+i,27+i%4,10+i//4,hp=40,team='defender') for i in range(8)]
        turn = Turn.load(p)
        remaining = {r.robot_id:r.health for r in turn.robots}
        old = {t.unit_id:namespace['select_targets'](turn,t,remaining) for t in turn.weapons()}
        start = time.perf_counter()
        new, _ = plan_fire(turn,turn.weapons())
        times.append(time.perf_counter()-start)
        before, after = evaluate(turn,old), evaluate(turn,new)
        old_total = [a+b for a,b in zip(old_total,before)]
        new_total = [a+b for a,b in zip(new_total,after)]
        outcomes[0 if after[0]>before[0] else 1 if after[0]==before[0] else 2] += 1
    print(json.dumps(dict(seed=20260913, scenarios=40,
        metrics=['own_wave_effective_damage','opponent_wave_damage','overkill_damage'],
        previous=old_total, new=new_total, own_damage_win_tie_loss=outcomes,
        mean_ms=round(sum(times)/len(times)*1000,2), max_ms=round(max(times)*1000,2))))


if __name__ == '__main__':
    main()
