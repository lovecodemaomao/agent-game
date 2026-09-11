# -*- coding: utf-8 -*-
"""判题侧 LLM 代理（任务7.2/7.3）。

- 承接 agent response.prompt -> 异步转发 deepseek -> 下回合 request.llmResp 回填
- 三上下文隔离：我方/对手 agent 各自独立消息历史；新闻系统独立上下文（见 news 模块）
- 每日配额：每队每游戏日 3 次非任务期调用；任务期（phaseTask 非空）豁免不计数
- 超限：不转发，向该队下一回合 request.errors 写入 errorCode=5
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional

from common import rules as R
from sim.judge.llmproxy import DeepSeekClient

log = logging.getLogger("judge.llmproxy")

AGENT_SYSTEM = "你是《未来战争》对战游戏中一支队伍的战术顾问。回答简洁、结构化。"


class LLMProxy:
    def __init__(self, llm_cfg: Dict[str, Any], provider=None, daily_quota: int = 3,
                 keep_history: bool = False):
        self.client = DeepSeekClient(llm_cfg)
        self.provider = provider
        self.daily_quota = daily_quota
        self.keep_history = keep_history
        self.executor = ThreadPoolExecutor(max_workers=4)
        # 每队独立上下文：互不可见
        self.history: Dict[str, list] = {"challenger": [], "defender": []}
        self.quota: Dict[str, Dict] = {t: {"day": 0, "used": 0} for t in ("challenger", "defender")}
        self.errors: Dict[str, list] = {"challenger": [], "defender": []}
        self._pending: Dict[str, Any] = {}  # team -> deque[Future]（FIFO，保序回填）
        self.stats = {t: {"total": 0, "quota_used": 0, "rejected": 0} for t in ("challenger", "defender")}

    # ------------------------------------------------------------------
    def on_round(self, round_no: int, round_recs: Dict[str, Any]) -> None:
        """按《接口文档》语义回填 llmResp: 本回合的 prompt 在回合内调用 LLM，
        下一回合 request.llmResp 返回其结果（同步等待，与真实判题器一致）。

        早期实现用后台线程+队列异步回填：回合推进远快于 LLM 延迟时，
        请求方收到的是"迟到的、非上一回合"的响应，破坏了接口文档约定的
        1 回合延迟，使按 request_id 校验回复的 agent 永远匹配不上。
        """
        day = R.day_of(round_no)
        for team, rec in round_recs["teams"].items():
            prompt = (rec.get("response") or {}).get("prompt", "")
            if not prompt:
                continue
            in_task = bool((rec.get("request") or {}).get("phaseTask"))
            if not in_task:
                q = self.quota[team]
                if q["day"] != day:
                    q["day"], q["used"] = day, 0
                if q["used"] >= self.daily_quota:
                    self.stats[team]["rejected"] += 1
                    self.errors[team].append({
                        "errorCode": 5,
                        "description": f"LLM额度超限：今日已用{q['used']}/{self.daily_quota}",
                    })
                    log.warning("[%s] 第%d回合 prompt 超限拒绝", team, round_no)
                    continue
                q["used"] += 1
                self.stats[team]["quota_used"] += 1
            self.stats[team]["total"] += 1
            try:
                resp = self._call(team, prompt)
            except Exception as e:
                resp = f"[LLM_ERROR] {e}"
            if self.provider is not None:
                self.provider.llm_resp[team] = resp

    def errors_for(self, team: str):
        errs = self.errors[team]
        self.errors[team] = []
        return errs

    # ------------------------------------------------------------------
    def _call(self, team: str, prompt: str) -> str:
        messages = []
        if self.keep_history:
            messages.extend(self.history[team])
        messages.append({"role": "user", "content": prompt})
        resp = self.client.chat(messages, purpose=f"agent:{team}")
        if self.keep_history:
            self.history[team].append({"role": "user", "content": prompt})
            self.history[team].append({"role": "assistant", "content": resp})
            # 上下文上限保护：保留最近20条
            self.history[team] = self.history[team][-20:]
        return resp

    # -- 新闻系统专用入口（独立上下文，不与 agent 互通）--
    def news_chat(self, prompt: str) -> str:
        return self.client.chat([{"role": "user", "content": prompt}], purpose="news")
