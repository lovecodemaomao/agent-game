"""自进化任务求解（修复"开拓者不做任务"）。

链路: 开拓者站到任务点 -> acceptTask -> 平台返回 phaseTask 描述
  -> 解析任务(如天气查询 API) -> 经 response.prompt 请教平台 LLM
     (任务期平台不限额) + 经 response.executeCmd 在沙盒内探测
  -> 次回合从 lastCmdResult 取结果 -> submitAnswer
  -> 首次探索成功后把可复用脚本经 executeCmd 落盘为 SOP，同类任务 2 回合零 LLM 完成。

不直连任何 LLM API: 所有 LLM 交互都走平台提供的 prompt/llmResp 通道。
"""
import base64
import json
import re
from typing import Any

SOP_SCRIPT = "sop_weather.sh"
SOP_CONTENT = """#!/bin/sh
# SOP: 天气查询任务（首次探索成功后沉淀，可被后续同类任务直接复用）
city="$1"
curl -s -m 8 "$MOCK_API_URL/weather?city=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$city")"
"""


class TaskAgent:
    """进程级单例使用: 记录当前任务实例状态与已学会的任务类型。"""

    def __init__(self) -> None:
        self.task: dict[str, Any] | None = None
        self.learned: set[str] = set()

    # ------------------------------------------------------------------
    def step(self, turn) -> tuple[dict[str, Any] | None, str, str]:
        """返回 (开拓者指令, prompt, executeCmd)。"""
        text = turn.phase_task
        if not text:
            self.task = None
            return None, "", ""
        if self.task is None or self.task.get("desc") != text:
            self.task = self._parse(text)
            self.task["desc"] = text
        state = self.task

        # 已拿到答案 -> 提交；首次任务同时沉淀 SOP 脚本
        if state.get("answer"):
            command = {"action": "submitAnswer",
                       "taskAnswer": "%s今天天气：%s" % (state["city"], state["answer"])}
            if state.pop("persist_sop", False):
                blob = base64.b64encode(SOP_CONTENT.encode()).decode()
                script = "echo %s | base64 -d > %s && chmod +x %s" % (blob, SOP_SCRIPT, SOP_SCRIPT)
                self.learned.add("weather")
                return command, "", script
            return command, "", ""

        # SOP 快速通道: 直接跑已沉淀脚本
        if state.get("sop"):
            if state["phase"] == "run_sop":
                state["phase"] = "await"
                return None, "", 'bash %s "%s"' % (SOP_SCRIPT, state["city"])
            if state["phase"] == "await":
                answer = extract_weather(turn.last_cmd_result)
                if answer:
                    state["answer"] = answer
                    return self.step(turn)
                state["phase"] = "run_sop"  # 失败重试
                return self.step(turn)

        # 首次探索: prompt(请教 LLM) 与 executeCmd(沙盒探测) 双通道并行
        if state.get("phase") is None:
            state["phase"] = "explore"
            prompt = ("你是游戏中的任务代理。任务：%s\n"
                      "请给出用 curl 完成该查询的一条 shell 命令，只输出命令本身。"
                      % text)
            return None, prompt, state["probe"]

        if state["phase"] == "explore":
            answer = extract_weather(turn.last_cmd_result)
            if answer:
                state["answer"] = answer
                state["persist_sop"] = "weather" not in self.learned
                return self.step(turn)
            retry = state.get("retry", 0) + 1
            state["retry"] = retry
            if retry > 2:
                state["answer"] = "未知"  # 兜底提交，避免拖到任务超时
                return self.step(turn)
            # 依据 LLM 回复修正探测命令（平台 llmResp 通道）
            command = extract_curl(turn.llm_resp)
            if command:
                state["probe"] = command
            state["phase"] = None
            return self.step(turn)

        return None, "", ""

    # ------------------------------------------------------------------
    def _parse(self, text: str) -> dict[str, Any]:
        match_city = re.search(r"查询(.+?)(?:今天|的天气)", text)
        match_url = re.search(r"GET (\S+?)/weather", text)
        city = match_city.group(1).strip() if match_city else "北京"
        url = match_url.group(1).strip() if match_url else "http://127.0.0.1:1"
        sop = "weather" in self.learned
        return {
            "city": city,
            "probe": probe_command(url, city),
            "sop": sop,
            "phase": "run_sop" if sop else None,
            "answer": None,
            "retry": 0,
        }


# ---------------------------------------------------------------- 工具
def probe_command(url: str, city: str) -> str:
    """沙盒内查询命令（URL 编码由 python 处理，避免中文与引号问题）。

    注意: $(...) 内的双引号由 shell 重新解析，禁止用反斜杠转义，
    否则城市名会带上字面引号导致 API 查不到。
    """
    quote = 'python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))"'
    return 'curl -s -m 8 "%s/weather?city=$(%s "%s")"' % (url, quote, city)


def extract_weather(last_cmd_result: str) -> str | None:
    """从 lastCmdResult 里解析天气（返回格式: [exitCode:N]\\n{...json...}）。"""
    if not last_cmd_result:
        return None
    body = re.sub(r"^\[(exitCode:\d+|TIMEOUT|JUDGER_ERROR)\]\n?", "", last_cmd_result)
    try:
        start = body.index("{")
        data = json.loads(body[start:body.rindex("}") + 1])
        weather = str(data.get("weather") or "")
        if weather and weather != "未知":
            return weather
    except (ValueError, json.JSONDecodeError):
        pass
    return None


def extract_curl(llm_resp: str) -> str | None:
    """从 LLM 回复里提取一条 curl 命令（仅接受 curl 开头的单行命令）。"""
    if not llm_resp:
        return None
    for line in llm_resp.splitlines():
        line = line.strip().strip("`").strip()
        if line.startswith("curl ") and len(line) < 400:
            return line
    return None
