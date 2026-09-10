# -*- coding: utf-8 -*-
"""我方 agent 入口：`python -m agents.competitor.main --port 18080`。

以仓库根为工作目录运行（sys.path 已含根），进程常驻直至被终止。
"""
from __future__ import annotations

import argparse
import logging
import threading

from agents.framework.llmchannel import LLMChannel
from agents.framework.orchestrator import Orchestrator
from agents.framework.server import serve

log = logging.getLogger("competitor")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", default="config/engine.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from agents.competitor.agent import CompetitorAgent  # 延迟导入便于独立测试
    agent = CompetitorAgent(config_path=args.config)
    orch = Orchestrator(agent, LLMChannel(daily_quota=agent.daily_quota),
                        round_budget=agent.round_budget,
                        on_dropped=lambda r, ds: log.warning("round %s dropped: %s", r, ds))
    serve(args.port, orch.handle)
    log.info("competitor agent ready on port %s", args.port)

    threading.Event().wait()  # 常驻


if __name__ == "__main__":
    main()
