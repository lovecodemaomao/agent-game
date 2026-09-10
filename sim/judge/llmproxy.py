# -*- coding: utf-8 -*-
"""DeepSeek API 客户端（任务7.1）。

- 密钥从仓库根 deepseek-key.md 读取（不入库）
- 指数退避重试 / 超时 / 磁盘缓存（同 prompt 去重，开发期省额度）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from sim.engine.config import repo_path

log = logging.getLogger("sim.llm")


def read_api_key(path: Optional[str] = None) -> Optional[str]:
    path = path or repo_path("deepseek-key.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("sk-"):
                    return line
    except FileNotFoundError:
        return None
    return None


class DeepSeekClient:
    def __init__(self, cfg: Dict[str, Any], cache_dir: Optional[str] = None):
        self.model = cfg.get("model", "deepseek-v4.1-flash")
        self.base_url = cfg.get("base_url", "https://api.deepseek.com")
        self.timeout = cfg.get("timeout", 60)
        self.retries = cfg.get("retries", 2)
        self.cache_enabled = cfg.get("cache", True)
        self.api_key = read_api_key()
        self.cache_dir = cache_dir or repo_path(".llmcache")
        if self.cache_enabled:
            os.makedirs(self.cache_dir, exist_ok=True)
        self.available = self.api_key is not None
        if not self.available:
            log.warning("未找到 deepseek-key.md 或其中无有效 key，LLM 调用将返回占位响应")

    # ------------------------------------------------------------------
    def chat(self, messages: List[Dict[str, str]], purpose: str = "default") -> str:
        """一次性补全。messages: [{"role","content"}...]"""
        cache_key = self._cache_key(messages, purpose)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        if not self.available:
            resp = f"[MOCK_LLM] {purpose}: 未配置API Key，占位响应"
            self._cache_put(cache_key, resp)
            return resp
        body = {"model": self.model, "messages": messages}
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=self.timeout)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                self._cache_put(cache_key, content)
                return content
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("LLM 调用失败(第%d次): %s，%ds后重试", attempt + 1, e, wait)
                time.sleep(wait)
        raise RuntimeError(f"LLM 调用最终失败: {last_err}")

    # ------------------------------------------------------------------
    def _cache_key(self, messages, purpose) -> str:
        h = hashlib.sha256()
        h.update(purpose.encode())
        h.update(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode())
        return h.hexdigest()

    def _cache_get(self, key) -> Optional[str]:
        if not self.cache_enabled:
            return None
        fp = os.path.join(self.cache_dir, key + ".txt")
        if os.path.isfile(fp):
            with open(fp, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def _cache_put(self, key, content: str) -> None:
        if not self.cache_enabled:
            return
        fp = os.path.join(self.cache_dir, key + ".txt")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
