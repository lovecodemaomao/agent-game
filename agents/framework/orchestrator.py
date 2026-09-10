# -*- coding: utf-8 -*-
"""回合编排器：时限内必答 + 出站校验 + 兜底。

流程：收到 request -> 线程内运行策略 decide（带时间预算）->
超时/异常则回退"上回合合法指令" -> 校验器清洗 -> 返回。
进程永不因单回合错误崩溃。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from agents.framework.llmchannel import LLMChannel
from agents.framework.validator import ValidationContext, validate_response


class Orchestrator:
    def __init__(self, strategy: Any, llm: LLMChannel,
                 round_budget: float = 3.5,
                 on_dropped: Optional[Callable[[int, List[str]], None]] = None):
        self.strategy = strategy
        self.llm = llm
        self.round_budget = round_budget
        self.on_dropped = on_dropped
        self._last_clean: Dict[str, Dict[str, Any]] = {}  # 上回合出站的合法指令
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        round_no = int(request.get("roundNo", 1))
        self.llm.observe(request)

        produced: Dict[str, Any] = {}
        error: List[str] = []

        def work():
            try:
                produced.update(self.strategy.decide(request) or {})
            except Exception as e:  # 兜底：任何策略异常不外泄
                error.append(f"{type(e).__name__}: {e}")

        t = threading.Thread(target=work, daemon=True)
        t0 = time.time()
        t.start()
        t.join(self.round_budget)
        timed_out = t.is_alive()
        if timed_out or error:
            produced = {}  # 弃用半成品/异常产物，走兜底

        resp = self._finalize(request, produced, timed_out, error, round_no)
        return resp

    def _finalize(self, request: Dict[str, Any], produced: Dict[str, Any],
                  timed_out: bool, error: List[str], round_no: int) -> Dict[str, Any]:
        ctx = ValidationContext(request)

        # prompt 通道：策略想发且配额允许才写入
        prompt = produced.get("prompt", "") or ""
        if prompt and not self.llm.send(prompt):
            prompt = ""
        produced["prompt"] = prompt

        clean, dropped = validate_response(produced, ctx)
        if dropped and self.on_dropped:
            try:
                self.on_dropped(round_no, dropped)
            except Exception:
                pass

        if timed_out or error or not clean["roleCommandMap"]:
            # 兜底：重放"上回合合法指令"中在当前上下文仍合法的部分
            replay = {"roleCommandMap": dict(self._last_clean), "prompt": "", "executeCmd": ""}
            replay, more = validate_response(replay, ctx)
            if more:
                dropped = dropped + more
            if timed_out:
                dropped = dropped + [f"round {round_no} 策略超时({self.round_budget}s)，已回退兜底"]
            if error:
                dropped = dropped + [f"round {round_no} 策略异常: {error[0]}"]
            clean = replay

        # executeCmd 仅任务期允许由策略设置；此处信任策略（框架不判任务态外泄）
        with self._lock:
            self._last_clean = dict(clean["roleCommandMap"])
        return clean

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {"llm": self.llm.state(), "last_commands": len(self._last_clean)}
