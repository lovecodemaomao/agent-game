# -*- coding: utf-8 -*-
"""引擎作为判题循环状态源的适配层（任务4.9）。

协议：build_request(team, round) / consume(team, round, response, meta) / finished(round)。
consume 缓存双方 response，收齐后推进引擎一步。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from common import rules as R
from sim.engine.config import EngineConfig
from sim.engine.resolve import Resolver
from sim.engine.state import WorldState
from sim.engine.view import build_request


class EngineProvider:
    def __init__(self, cfg: EngineConfig, seed: int = 42, news_llm=None):
        self.cfg = cfg
        self.w = WorldState.create(cfg, seed=seed)
        self.res = Resolver(cfg, seed=seed)
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._last_round = 0
        self.llm_resp: Dict[str, str] = {}
        self.base_destroyed: Dict[str, bool] = {"challenger": False, "defender": False}
        # 新闻系统（参数先行，LLM 措辞可选）
        from sim.news.system import NewsSystem
        self.news = NewsSystem(cfg, seed=seed, width=cfg.width, height=cfg.height,
                               llm_chat=news_llm)
        self.w.treasure = self.news.treasure
        self._news_day = 0
        self.news.new_day(1, self.w)  # 第1天新闻
        self._news_day = 1
        # mock 任务 API + 任务描述注入
        from sim.sandbox.mockapi import MockAPI
        self.mock_api = MockAPI()
        self.res.task_api_url = self.mock_api.url

    def in_task(self, team: str) -> bool:
        return bool(self.w.phase_task.get(team))

    def task_api_url(self) -> str:
        return self.mock_api.url

    # -- 判题循环协议 --
    def build_request(self, team: str, round_no: int, errors=None) -> Dict[str, Any]:
        req = build_request(self.w, self.cfg, team,
                            llm_resp=self.llm_resp.pop(team, ""),
                            errors=errors)  # 送达即清，避免黏滞
        return req

    def consume(self, team: str, round_no: int, response: Dict[str, Any], meta: Dict[str, Any]) -> None:
        self._pending[team] = response
        if len(self._pending) < 2:
            return
        # 双方收齐 -> 引擎推进一步
        commands = {t: (self._pending.get(t) or {}).get("roleCommandMap", {})
                    for t in ("challenger", "defender")}
        self.res.step(self.w, commands)
        for t in ("challenger", "defender"):
            b = next((u for u in self.w.base_units(t)), None)
            if b is None:
                self.base_destroyed[t] = True
        self._pending.clear()
        # 新闻：当日影响结算 + 次日预告
        self.news._apply_event_effects(R.day_of(self.w.round_no), self.w)
        next_day = R.day_of(self.w.round_no + 1)
        if next_day != self._news_day and next_day <= 10:
            self.news.new_day(next_day, self.w)
            self._news_day = next_day

    @property
    def last_events(self):
        return self.w.events

    def finished(self, round_no: int) -> Optional[str]:
        if self.base_destroyed["challenger"] and self.base_destroyed["defender"]:
            return "both_destroyed"
        if round_no >= R.MAX_ROUNDS:
            return "rounds_exhausted"
        return None

    # -- 终局判定（任务书第七章）--
    def match_result(self) -> Dict[str, Any]:
        w = self.w
        destroyed = {t: w.base_destroyed_round[t] for t in ("challenger", "defender")}
        scores = {t: w.teams[t].total_score for t in ("challenger", "defender")}
        if destroyed["challenger"] and destroyed["defender"]:
            if destroyed["challenger"] == destroyed["defender"]:
                winner = "tie"
            else:
                winner = "challenger" if destroyed["challenger"] > destroyed["defender"] else "defender"
        elif destroyed["challenger"]:
            winner = "defender"
        elif destroyed["defender"]:
            winner = "challenger"
        else:
            winner = "challenger" if scores["challenger"] > scores["defender"] else \
                "defender" if scores["defender"] > scores["challenger"] else "tie"
        return {
            "winner": winner,
            "scores": scores,
            "score_detail": {t: {"task": w.teams[t].score_task, "kill": w.teams[t].score_kill,
                                 "survive": w.teams[t].score_survive} for t in ("challenger", "defender")},
            "base_destroyed_round": destroyed,
            "kills": {t: w.teams[t].score_kill for t in ("challenger", "defender")},
        }
