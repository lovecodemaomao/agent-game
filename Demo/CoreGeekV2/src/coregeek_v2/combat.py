"""Compose fire for scheduler-owned operators without assigning new roles."""
from itertools import product
from .fire_control import plan_fire, score_plan, combat_context, SURVIVAL_MODE
from .jobs import DefenseJob, action
from .model import Lifecycle


def prepare_fire(world, state):
    if world.is_day:
        return
    records = [r for r in state.jobs.values() if r.lifecycle == Lifecycle.ACTIVE and isinstance(r.behavior, DefenseJob)]
    options = [(None,) + r.behavior.combat_candidates(world, r.role_id) for r in records]
    context = combat_context(world, SURVIVAL_MODE)
    best, best_score, cache = (), None, {}
    for combination in product(*options):
        towers = tuple(t for t in combination if t is not None)
        if len({t.id for t in towers}) != len(towers):
            continue
        key = tuple(sorted(t.id for t in towers))
        if key not in cache:
            plan, _ = plan_fire(world, towers, context=context)
            cache[key] = plan, score_plan(world, towers, plan, context)
        plan, score = cache[key]
        if best_score is None or score > best_score:
            best_score, best = score, (combination, plan)
    if best:
        combination, plan = best
        for record, tower in zip(records, combination):
            if tower and plan.get(tower.id):
                record.behavior.fire_intent = action('attack', _weapon_id=tower.id,
                                                     targetPos=[p.dump() for p in plan[tower.id]])
