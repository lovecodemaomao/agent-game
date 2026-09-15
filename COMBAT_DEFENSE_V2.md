# Combat defense v2 work log

Baseline: zayx1228/agent-game main 176c961.

Implementation: preserve initial-state ballistics; add bounded combat scoring,
clear feasibility and latched survival mode; evaluate repair and operator alternatives;
stage walls, persist a daytime L1 gate and repair/rebuild jobs; revise upgrade priorities.
No task/LLM/news/mining algorithm rewrite. Update deploy archive after validation.

Baseline checks: 183 passed, 1 failed (test_night_moves_to_inner_side_when_adjacent_outside).
That test assumes every detour step strictly approaches the base; inspect against
actual reachability rather than delete it. This result predates all implementation.

Rule boundaries: one controller per weapon per round; construction daytime only;
rocket cooldown three empty rounds; start-of-round ballistic HP. Robot routing and
same-round incoming attack order remain conservative benchmark assumptions.
