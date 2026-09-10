# -*- coding: utf-8 -*-
"""我方 agent（任务组11）：OpponentModel + 教义×旋钮 + 每日LLM分析 + 任务代理。

- 日常回合：零 LLM，确定性引擎按当前教义参数调度
- 每日第1回合：发合并分析 prompt（仪表盘+教义索引+新闻），llmResp 到达后
  校验+夹紧 -> 原子热替换当日参数；失败走兜底链（昨日教义 -> 全局默认）
- 任务期：TaskAgent 全权接管开拓者（LLM 无限额）
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, Optional

from agents.competitor.doctrine import DoctrineParams, doctrine_index, load_doctrine
from agents.competitor.engine import CompetitorEngine
from agents.competitor.opponent_model import OpponentModel
from agents.framework.llmchannel import LLMChannel
from agents.framework.taskagent import TaskAgent
from common import rules as R

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class CompetitorAgent:
    daily_quota = 3
    round_budget = 3.5

    def __init__(self, config_path: str = "config/engine.yaml", doctrine: str = "balanced"):
        self.current: DoctrineParams = load_doctrine(doctrine)   # 当前生效参数
        self.default_doctrine = doctrine
        self.engine = CompetitorEngine()
        self.opponent = OpponentModel("challenger")
        self.task_agent = TaskAgent(LLMChannel(daily_quota=0))
        self.llm = LLMChannel(self.daily_quota)
        self._analysis_sent_day = 0

    # ==================================================================
    def decide(self, request: Dict[str, Any]) -> Dict[str, Any]:
        round_no = int(request["roundNo"])
        day = R.day_of(round_no)
        self.llm.observe(request)  # 日界配额重置与 outstanding 清理必须每回合执行
        self.opponent.update(request)

        # 每日LLM分析结果回填（llmResp 是昨日/今日发出的分析的回答）
        self._consume_analysis(request)

        # 任务期：TaskAgent 接管
        if request.get("phaseTask"):
            pv = next((r for r in request["teamOur"]["roles"] if r["roleType"] == "pioneer"), None)
            if pv:
                ta = self.task_agent.step(request, pv["id"])
                rest = self.engine.decide(request, self.current)
                rest["roleCommandMap"].update(ta.get("commands", {}))
                # 移除开拓者的日常指令（任务代理全权）
                rest["roleCommandMap"].pop(str(pv["id"]), None)
                rest["prompt"] = ta.get("prompt", "")
                rest["executeCmd"] = ta.get("executeCmd", "")
                return rest

        # 日界：发当日分析 prompt（每日3次预算的第1发）
        out = self.engine.decide(request, self.current)
        if R.is_day_first_round(round_no) and self._analysis_sent_day < day:
            analysis = self._build_analysis_prompt(request, day)
            if self.llm.send(analysis):
                out["prompt"] = analysis
                self._analysis_sent_day = day
        return out

    # ------------------------------------------------------------------
    def _build_analysis_prompt(self, request: Dict[str, Any], day: int) -> str:
        dash = self.opponent.dashboard_text()
        idx = json.dumps(doctrine_index(), ensure_ascii=False)
        news = request.get("worldNews", {})
        ours = request["teamOur"]
        return (
            "你是《未来战争》我方队伍的每日策略分析师。请基于以下信息选择今日教义并微调旋钮。\n"
            f"== 当前局面 ==\n金币{ours['goldNum']} 积分{ours['totalScore']} "
            f"单位{len(ours['roles'])}\n"
            f"== 对手画像 ==\n{dash}\n"
            f"== 今日新闻 ==\n官方：{news.get('officialNews','')}\n传闻：{news.get('folkLegends','')}\n"
            f"== 教义索引 ==\n{idx}\n"
            '只输出JSON（不要多余文本）：{"doctrine": "<索引中的name>", '
            '"overrides": {"<旋钮>": <数值>}, "reason": "<一句话>"}')
        # 旋钮范围说明省略以省上下文：非法值会被夹紧

    def _consume_analysis(self, request: Dict[str, Any]) -> None:
        resp = request.get("llmResp", "") or ""
        if not resp:
            return
        parsed = self._parse_analysis(resp)
        if parsed is None:
            return  # 兜底链：保持昨日教义（继续用当前参数）
        new_params = self.current.copy()
        new_params.name = parsed.get("doctrine", new_params.name)
        try:
            base = load_doctrine(new_params.name)  # 教义切换：以预设卡为底
        except FileNotFoundError:
            base = self.current.copy()
        new_params = base.apply_overrides(parsed.get("overrides", {}))
        self.current = new_params  # 原子热替换

    @staticmethod
    def _parse_analysis(resp: str) -> Optional[Dict[str, Any]]:
        m = re.search(r"\{.*\}", resp, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict) or "doctrine" not in d:
            return None
        return d

    def stats(self) -> Dict[str, Any]:
        return {"doctrine": self.current.name,
                "opponent": self.opponent.features()}
