# -*- coding: utf-8 -*-
"""一场完整对战的编排入口。

用法：
  python -m sim.judge.runmatch --rounds 1300 --record matches/m1.jsonl [--seed 42]
                               [--defender-card config/baselines/turtle_defense.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

from sim.engine.config import load_config, repo_path
from sim.engine.provider import EngineProvider
from sim.judge.agentproc import start_agents, stop_agents
from sim.judge.loop import JudgeLoop

log = logging.getLogger("judge.runmatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1300)
    parser.add_argument("--record", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--defender-card", default="config/baselines/turtle_defense.json")
    parser.add_argument("--response-deadline", type=float, default=5.0)
    parser.add_argument("--swap-seats", action="store_true",
                        help="挑战者=基线agent，防守者=我方agent（换边对战）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config()
    record = args.record or repo_path("matches", f"match_{int(time.time())}.jsonl")
    os.makedirs(os.path.dirname(record), exist_ok=True)

    provider = EngineProvider(cfg, seed=args.seed, news_llm=None)
    # 每队独立沙盒（持久家目录 + mock API 注入）
    from sim.sandbox.exec import Sandbox
    sandboxes = {t: Sandbox(t, timeout=cfg.sandbox["timeout"],
                            mock_api_url=provider.task_api_url())
                 for t in ("challenger", "defender")}
    # LLM 代理：三上下文隔离 + 每日配额（新闻系统独占 news 上下文）
    from sim.judge.proxy import LLMProxy
    llm_proxy = LLMProxy(cfg.llm, provider=provider,
                         daily_quota=cfg.llm["daily_quota"])
    provider.news.llm_chat = llm_proxy.news_chat  # 新闻措辞走独立上下文
    if args.swap_seats:
        agent_map = {"challenger": "agents.baseline.main",
                     "defender": "agents.competitor.main"}
        extra = {"challenger": ["--card", args.defender_card]}
    else:
        agent_map = {"challenger": "agents.competitor.main",
                     "defender": "agents.baseline.main"}
        extra = {"defender": ["--card", args.defender_card]}
    procs = start_agents(agent_map, extra_args=extra)
    try:
        jl = JudgeLoop(provider, {t: p.port for t, p in procs.items()},
                       response_deadline=args.response_deadline,
                       max_abnormal=cfg.judge["max_abnormal"],
                       record_path=record, sandboxes=sandboxes,
                       llm_proxy=llm_proxy)
        result = jl.run(max_rounds=args.rounds)
    finally:
        stop_agents(procs)

    summary = {"match_result": provider.match_result(), "judge": result,
               "record": record, "seed": args.seed}
    summary_path = record.replace(".jsonl", "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log.info("summary -> %s", summary_path)
    print(json.dumps(summary["match_result"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
