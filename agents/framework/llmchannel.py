# -*- coding: utf-8 -*-
"""LLM 异步通道（agent 侧）。

- 发送：把文本写入 response.prompt 字段，由判题器代理调用 LLM
- 接收：下一回合 request.llmResp 携带结果（1 回合延迟）
- 配额：每游戏日 3 次非任务期调用（本地自律，判题器侧强制）；
  自进化任务期间（phaseTask 非空）无限额且不计数
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from common import rules as R


class LLMChannel:
    def __init__(self, daily_quota: int = 3):
        self.daily_quota = daily_quota
        self._day: int = 0
        self._used_today: int = 0
        self._outstanding: bool = False  # 已发出尚未收到回复
        self.last_resp: str = ""
        self.in_task: bool = False

    # -- 每回合由编排器调用 --
    def observe(self, request: Dict[str, Any]) -> None:
        round_no = int(request.get("roundNo", 1))
        day = R.day_of(round_no)
        if day != self._day:
            self._day = day
            self._used_today = 0
            self._outstanding = False
        self.last_resp = request.get("llmResp", "") or ""
        if self.last_resp:
            self._outstanding = False
        task_text = request.get("phaseTask", "") or ""
        was_in_task = self.in_task
        self.in_task = bool(task_text)
        if self.in_task and not was_in_task:
            pass  # 任务期开始
        if not self.in_task:
            self._outstanding = False  # 任务结束，清理悬挂状态

    @property
    def can_send(self) -> bool:
        if self._outstanding:
            return False
        if self.in_task:
            return True
        return self._used_today < self.daily_quota

    @property
    def used_today(self) -> int:
        return self._used_today

    def send(self, prompt: str) -> bool:
        """登记一次 prompt 发送。返回是否允许（由编排器写入 response.prompt）。"""
        if not prompt or not self.can_send:
            return False
        if not self.in_task:
            self._used_today += 1
        self._outstanding = True
        return True

    def state(self) -> Dict[str, Any]:
        return {"day": self._day, "used": self._used_today, "outstanding": self._outstanding,
                "in_task": self.in_task}
