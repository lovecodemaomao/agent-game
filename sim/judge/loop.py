# -*- coding: utf-8 -*-
"""判题循环：逐回合驱动双方 agent、异常计数、对局记录。

state_provider 协议（引擎在任务组4接入）：
  provider.build_request(team, round_no) -> request dict（该队视角）
  provider.consume(team, round_no, response, meta) -> None（结算由引擎接管）
  provider.finished(round_no) -> bool / 结论字符串
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.framework.validator import ValidationContext, validate_response

log = logging.getLogger("judge.loop")


class TeamTracker:
    """单队异常计数与调度状态。"""

    def __init__(self, team: str, max_abnormal: int = 5):
        self.team = team
        self.max_abnormal = max_abnormal
        self.abnormal = 0
        self.suspended = False
        self.errors: List[Dict[str, Any]] = []

    def count_abnormal(self, code: int, desc: str) -> None:
        self.abnormal += 1
        self.errors.append({"round": None, "errorCode": code, "description": desc})
        log.warning("[%s] 异常 %d/%d: %s", self.team, self.abnormal, self.max_abnormal, desc)
        if self.abnormal >= self.max_abnormal:
            self.suspended = True
            log.error("[%s] 异常达上限，停止调度", self.team)


def _post(url: str, payload: Dict[str, Any], deadline: float) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=deadline) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_agent(url: str, payload: Dict[str, Any],
                  connect_timeout: float, deadline: float) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """返回 (response, error)。error ∈ None | 'timeout' | 'format'"""
    t0 = time.time()
    try:
        resp = _post(url, payload, deadline)
    except Exception as e:
        # 连接类失败统一按超时口径（赛制：建连>10s 或响应>5s 均为超时）
        log.warning("agent %s 请求失败: %s (%.1fs)", url, e, time.time() - t0)
        return None, "timeout"
    if time.time() - t0 > deadline + 0.5:
        return None, "timeout"
    if not isinstance(resp, dict) or not {"roleCommandMap", "prompt", "executeCmd"} <= set(resp):
        return None, "format"
    return resp, None


class JudgeLoop:
    def __init__(self, provider: Any, ports: Dict[str, int],
                 connect_timeout: float = 10.0, response_deadline: float = 5.0,
                 max_abnormal: int = 5, record_path: Optional[str] = None,
                 llm_proxy: Optional[Any] = None, sandboxes: Optional[Dict[str, Any]] = None):
        self.provider = provider
        self.ports = ports
        self.connect_timeout = connect_timeout
        self.deadline = response_deadline
        self.trackers = {t: TeamTracker(t, max_abnormal) for t in ports}
        self.record_path = record_path
        self._fh = open(record_path, "w", encoding="utf-8") if record_path else None
        self.llm_proxy = llm_proxy
        self.sandboxes = sandboxes or {}
        self.round = 0
        try:
            import inspect
            self._provider_accepts_errors = "errors" in inspect.signature(provider.build_request).parameters
        except (TypeError, ValueError):
            self._provider_accepts_errors = False

    # ------------------------------------------------------------------
    def _record(self, obj: Dict[str, Any]) -> None:
        if self._fh:
            self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _sanitize(self, team: str, resp: Dict[str, Any], request: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """按《接口文档》校验 agent response；非法指令剔除并计"指令错误"异常。"""
        ctx = ValidationContext(request)
        clean, dropped = validate_response(resp, ctx)
        n_illegal = sum(1 for d in dropped if "剔除" not in d)
        return clean, n_illegal

    def run(self, max_rounds: int = 1300) -> Dict[str, Any]:
        t_start = time.time()
        conclusion = "rounds_exhausted"
        for self.round in range(1, max_rounds + 1):
            if self.provider.finished(self.round):
                conclusion = self.provider.finished(self.round)
                break
            round_recs: Dict[str, Any] = {"roundNo": self.round, "teams": {}}
            for team, port in self.ports.items():
                tr = self.trackers[team]
                errs = self.llm_proxy.errors_for(team) if self.llm_proxy is not None else []
                request = (self.provider.build_request(team, self.round, errors=errs)
                           if self._provider_accepts_errors
                           else self.provider.build_request(team, self.round))
                rec: Dict[str, Any] = {"request": request, "abnormal": None}
                if tr.suspended:
                    rec["response"] = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
                    rec["skipped"] = "suspended"
                else:
                    url = f"http://127.0.0.1:{port}/"
                    resp, err = request_agent(url, request, self.connect_timeout, self.deadline)
                    if err == "timeout":
                        tr.count_abnormal(3, "响应超时")
                        rec["abnormal"] = "timeout"
                        resp = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
                    elif err == "format":
                        tr.count_abnormal(2, "响应格式错误")
                        rec["abnormal"] = "format"
                        resp = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
                    else:
                        clean, n_illegal = self._sanitize(team, resp, request)
                        if n_illegal:
                            tr.count_abnormal(4, f"指令错误 x{n_illegal}")
                            rec["abnormal"] = f"illegal x{n_illegal}"
                        rec["dropped"] = [d for d in (rec.get("dropped") or [])]
                        resp = clean
                    # 沙盒执行：仅任务期允许（9.3 门控）
                    cmd_str = (resp.get("executeCmd") or "").strip()
                    if cmd_str:
                        sb = self.sandboxes.get(team)
                        in_task = getattr(self.provider, "in_task", lambda t: True)(team)
                        if sb is None or not in_task:
                            rec["executeCmd_ignored"] = cmd_str
                            resp["executeCmd"] = ""
                        else:
                            result = sb.execute(cmd_str)
                            self.provider.w.last_cmd_result[team] = result
                            rec["executeCmd_result"] = result[:400]
                    rec["response"] = resp
                self.provider.consume(team, self.round, rec["response"], rec)
                round_recs["teams"][team] = rec
            # LLM 代理：异步转发 prompt（任务组7接入，此处保留挂点）
            if self.llm_proxy is not None:
                self.llm_proxy.on_round(self.round, round_recs)
            round_recs["events"] = list(getattr(self.provider, "last_events", []))
            self._record(round_recs)
            if self.round % 130 == 0:
                log.info("round %d/%d (%.0fs)", self.round, max_rounds, time.time() - t_start)
        if self._fh:
            self._fh.close()
        result = {
            "conclusion": conclusion,
            "rounds": self.round,
            "abnormal": {t: tr.abnormal for t, tr in self.trackers.items()},
            "suspended": {t: tr.suspended for t, tr in self.trackers.items()},
            "wall_time": time.time() - t_start,
        }
        log.info("match finished: %s", result)
        return result

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.close()
