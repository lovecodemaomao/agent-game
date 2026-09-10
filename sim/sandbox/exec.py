# -*- coding: utf-8 -*-
"""每队独立沙盒执行器（任务9.1/9.2）。

- 子进程 shell 执行 executeCmd，15 秒超时
- 回填格式：`[exitCode:N]\n<输出>`；超时 `[TIMEOUT]\n<部分输出>`；>64KB 追加 `[TRUNCATED]`
- 禁网近似：http(s)_proxy/all_proxy 指向死端口（localhost 不受影响，可访问 mock API）
- 持久家目录：sandbox_runtime/<team>/，跨任务/跨天保留（SOP 沉淀依赖此性质）
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Dict, Optional

log = logging.getLogger("sim.sandbox")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_OUTPUT = 65536
TIMEOUT = 15


class Sandbox:
    def __init__(self, team: str, home: Optional[str] = None,
                 mock_api_url: str = "", timeout: int = TIMEOUT):
        self.team = team
        self.home = home or os.path.join(REPO, "sandbox_runtime", team)
        os.makedirs(self.home, exist_ok=True)
        self.mock_api_url = mock_api_url
        self.timeout = timeout

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if os.environ.get("SANDBOX_ALLOW_NET") != "1":
            dead = "http://127.0.0.1:1"
            for k in ("http_proxy", "https_proxy", "all_proxy",
                      "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                env[k] = dead
            env["no_proxy"] = "127.0.0.1,localhost"
            env["NO_PROXY"] = "127.0.0.1,localhost"
        if self.mock_api_url:
            env["MOCK_API_URL"] = self.mock_api_url
        env["SANDBOX_HOME"] = self.home
        return env

    def execute(self, cmd: str) -> str:
        """执行命令并按《接口文档》1.1 lastCmdResult 格式回填。"""
        try:
            proc = subprocess.Popen(
                ["/bin/zsh", "-c", cmd], cwd=self.home, env=self._env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True)
            try:
                out, _ = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                out, _ = proc.communicate()
                return self._format(b"[TIMEOUT]\n" + (out or b""))
            return self._format(f"[exitCode:{proc.returncode}]\n".encode() + (out or b""))
        except Exception as e:
            return f"[JUDGER_ERROR]\n{e}"

    @staticmethod
    def _format(raw: bytes) -> str:
        truncated = len(raw) > MAX_OUTPUT
        if truncated:
            raw = raw[:MAX_OUTPUT]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[TRUNCATED]"
        return text
