# -*- coding: utf-8 -*-
"""判题循环测试用 Demo 状态源：以 docs/request.txt 为模板逐回合递增 roundNo。

仅用于验证判题循环机制（3.2-3.4）；真实状态源是任务组4的引擎。
"""
from __future__ import annotations

import copy
from typing import Any, Dict

from tests.util import load_demo_request


class DemoProvider:
    def __init__(self, base_request: Dict[str, Any]):
        self.base = base_request

    def build_request(self, team: str, round_no: int) -> Dict[str, Any]:
        req = copy.deepcopy(self.base)
        req["roundNo"] = round_no
        return req

    def consume(self, team: str, round_no: int, response: Dict[str, Any], meta: Dict[str, Any]) -> None:
        pass

    def finished(self, round_no: int):
        return None
