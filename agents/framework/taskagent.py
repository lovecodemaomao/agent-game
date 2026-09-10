# -*- coding: utf-8 -*-
"""自进化任务求解代理（任务组10）。

状态机（每任务实例）：
  探索期(首次)：R1 发 prompt 请教 LLM + 不等待，直接 executeCmd 试探
                R2 读 llmResp/lastCmdResult -> submitAnswer + executeCmd 沉淀 SOP 脚本
  SOP期(同类复现)：R1 直接跑已沉淀脚本 executeCmd（零 LLM）
                R2 读 lastCmdResult -> submitAnswer
  提交成功 -> 状态清零；引擎侧 30 回合冷却后可再接

双通道并行：prompt 与 executeCmd 同回合发出（均为 1 回合延迟回填）。
SOP 脚本落在沙盒家目录（跨任务、跨进程持久）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

from agents.framework.llmchannel import LLMChannel

SOP_SCRIPT = "sop_weather.sh"
SOP_CONTENT = """#!/bin/zsh
# SOP: 天气查询任务（由首次任务探索沉淀）
city="$1"
curl -s -m 8 "$MOCK_API_URL/weather?city=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$city")"
"""


class TaskAgent:
    def __init__(self, llm: LLMChannel, sandbox_home: Optional[str] = None):
        self.llm = llm
        # 注意：沙盒文件系统在判题侧，agent 不能直接读写——
        # SOP 沉淀走 executeCmd（base64 落盘），SOP 感知走进程内 known_types
        self.task: Optional[Dict[str, Any]] = None   # 当前任务实例状态
        self.known_types: set = set()                 # 进程内已学会的任务类型

    # ------------------------------------------------------------------
    def step(self, request: Dict[str, Any], role_id: int) -> Dict[str, Any]:
        """返回本回合该角色（开拓者）的指令集合与 prompt。"""
        task_text = request.get("phaseTask", "") or ""
        last_result = request.get("lastCmdResult", "") or ""

        if not task_text:
            self.task = None
            return {"commands": {}, "prompt": ""}

        if self.task is None or self.task.get("desc") != task_text:
            self.task = self._parse_task(task_text)
            self.task["desc"] = task_text
        st = self.task

        out: Dict[str, Any] = {"commands": {}, "prompt": ""}

        # -- 已有答案待提交（同时用 executeCmd 沉淀 SOP 脚本）--
        if st.get("weather"):
            out["commands"][str(role_id)] = {
                "action": "submitAnswer",
                "taskAnswer": f"{st['city']}今天天气：{st['weather']}",
            }
            if st.get("first_task"):
                st["first_task"] = False
                import base64
                b64 = base64.b64encode(SOP_CONTENT.encode()).decode()
                out["executeCmd"] = (
                    f"echo {b64} | base64 -d > {SOP_SCRIPT} && chmod +x {SOP_SCRIPT}")
                self.known_types.add("weather")
            return out

        # -- SOP 快速通道：执行沉淀脚本（零 LLM，2 回合完成）--
        if st.get("sop_ready"):
            if st.get("phase") == "run_sop":
                out["executeCmd"] = f'bash {SOP_SCRIPT} "{st["city"]}"'
                st["phase"] = "await_result"
                return out
            if st["phase"] == "await_result":
                weather = self._extract_weather(last_result)
                if weather:
                    st["weather"] = weather
                    return self.step(request, role_id)
                # 失败重试一次
                st["phase"] = "run_sop"
                return self.step(request, role_id)

        # -- 探索通道（首次）--
        if st.get("phase") is None:
            st["phase"] = "explore"
            # 双通道并行：prompt 请教 LLM + executeCmd 直接试探
            if self.llm.can_send:
                out["prompt"] = (
                    f"你是游戏中的任务代理。任务：{task_text}\n"
                    "请给出用 curl 完成查询的一条 shell 命令，只输出命令本身。")
            py_quote = ('python3 -c "import urllib.parse,sys;'
                        'print(urllib.parse.quote(sys.argv[1]))"')
            out["executeCmd"] = (f'curl -s -m 8 "{st["url"]}/weather?city='
                                 f'$({py_quote} "{st["city"]}")"')
            return out

        if st["phase"] == "explore":
            weather = self._extract_weather(last_result)
            if weather:
                st["weather"] = weather
                st["first_task"] = "weather" not in self.known_types
                return self.step(request, role_id)
            # 探索失败：重试（prompt+executeCmd 双通道重发）
            st["phase"] = None
            st["retry"] = st.get("retry", 0) + 1
            if st["retry"] > 2:
                st["weather"] = "未知"  # 兜底提交，避免无限超时
            return self.step(request, role_id)

        return out

    # ------------------------------------------------------------------
    def _parse_task(self, text: str) -> Dict[str, Any]:
        m_city = re.search(r"查询(.+?)(?:今天|的天气)", text)
        m_url = re.search(r"GET (\S+?)/weather", text)
        city = m_city.group(1).strip() if m_city else "北京"
        url = m_url.group(1).strip() if m_url else os.environ.get("MOCK_API_URL", "")
        sop = "weather" in self.known_types  # 进程内已知 + 脚本经 executeCmd 沉淀在沙盒
        return {"type": "weather", "city": city, "url": url,
                "phase": "run_sop" if sop else None, "sop_ready": sop,
                "weather": None, "retry": 0}

    @staticmethod
    def _extract_weather(last_result: str) -> Optional[str]:
        if not last_result:
            return None
        # lastCmdResult 格式: [exitCode:0]\n{"city":..,"weather":..}
        body = re.sub(r"^\[(exitCode:\d+|TIMEOUT|JUDGER_ERROR)\]\n?", "", last_result)
        try:
            start = body.index("{")
            d = json.loads(body[start:body.rindex("}") + 1])
            w = d.get("weather")
            if w and w != "未知":
                return w
        except (ValueError, json.JSONDecodeError):
            pass
        return None
