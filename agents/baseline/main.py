# -*- coding: utf-8 -*-
"""对手基线 agent 入口：`python -m agents.baseline.main --port 18081`。"""
from __future__ import annotations

import argparse
import logging
import threading

from agents.framework.llmchannel import LLMChannel
from agents.framework.orchestrator import Orchestrator
from agents.framework.server import serve

log = logging.getLogger("baseline")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", default="config/engine.yaml")
    parser.add_argument("--card", default="config/baselines/turtle_defense.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from agents.baseline.agent import BaselineAgent
    agent = BaselineAgent(config_path=args.config, card_path=args.card)
    orch = Orchestrator(agent, LLMChannel(daily_quota=agent.daily_quota),
                        round_budget=agent.round_budget,
                        on_dropped=lambda r, ds: log.warning("round %s dropped: %s", r, ds))
    serve(args.port, orch.handle)
    log.info("baseline agent ready on port %s", args.port)

    threading.Event().wait()  # 常驻


if __name__ == "__main__":
    main()
