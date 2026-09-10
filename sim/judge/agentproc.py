# -*- coding: utf-8 -*-
"""agent 子进程编排：拉起、健康探测、优雅退出。"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional

log = logging.getLogger("judge.agentproc")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AgentProcess:
    def __init__(self, team: str, module: str, port: int):
        self.team = team
        self.module = module
        self.port = port
        self.proc: Optional[subprocess.Popen] = None

    def start(self, extra_args: Optional[List[str]] = None, wait: float = 15.0) -> None:
        cmd = [sys.executable, "-m", self.module, "--port", str(self.port)] + (extra_args or [])
        self.proc = subprocess.Popen(cmd, cwd=REPO_ROOT,
                                     stdout=open(f"/tmp/agent_{self.team}.log", "ab"),
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"agent {self.team} 提前退出，日志见 /tmp/agent_{self.team}.log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1) as r:
                    if r.status == 200:
                        log.info("agent %s ready on %s (pid %s)", self.team, self.port, self.proc.pid)
                        return
            except Exception:
                time.sleep(0.2)
        raise TimeoutError(f"agent {self.team} 健康探测超时")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
            log.info("agent %s stopped", self.team)


def start_agents(agent_specs: Dict[str, str], base_port: int = 18000,
                 extra_args: Optional[Dict[str, List[str]]] = None) -> Dict[str, AgentProcess]:
    """agent_specs: {team: module} -> {team: AgentProcess}"""
    procs = {}
    for i, (team, module) in enumerate(sorted(agent_specs.items())):
        ap = AgentProcess(team, module, base_port + i)
        ap.start((extra_args or {}).get(team))
        procs[team] = ap
    return procs


def stop_agents(procs: Dict[str, AgentProcess]) -> None:
    for ap in procs.values():
        try:
            ap.stop()
        except Exception:
            log.exception("stop agent %s failed", ap.team)
