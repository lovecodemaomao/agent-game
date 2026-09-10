# -*- coding: utf-8 -*-
"""引擎配置加载：config/engine.yaml -> EngineConfig（只读视图）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def repo_path(*parts: str) -> str:
    return os.path.join(REPO_ROOT, *parts)


@dataclass
class EngineConfig:
    raw: Dict[str, Any] = field(default_factory=dict)

    # -- 便捷访问 --
    @property
    def width(self) -> int:
        return self.raw["map"]["width"]

    @property
    def height(self) -> int:
        return self.raw["map"]["height"]

    def base_pos(self, team: str) -> Tuple[int, int]:
        return tuple(self.raw["map"][f"{team}_base"])

    def zone_ring(self, which: str) -> Tuple[int, int]:
        """可建造区域距离环带 [min, max]，which ∈ {weapon, wall}。"""
        return tuple(self.raw["map"][f"{which}_zone"])

    @property
    def vendor(self) -> Tuple[int, int]:
        return tuple(self.raw["map"]["vendor"])

    @property
    def weapon_shop(self) -> Tuple[int, int]:
        return tuple(self.raw["map"]["weapon_shop"])

    @property
    def task_points(self) -> Dict[str, Dict[str, List[Tuple[int, int]]]]:
        out = {}
        for team, points in self.raw["map"]["task_points"].items():
            out[team] = {k: [tuple(c) for c in v] for k, v in points.items()}
        return out

    @property
    def mine_count(self) -> Dict[str, int]:
        return dict(self.raw["map"]["mine_count"])

    @property
    def economy(self) -> Dict[str, Any]:
        return self.raw["economy"]

    @property
    def robots(self) -> Dict[str, Any]:
        return self.raw["robots"]

    @property
    def news(self) -> Dict[str, Any]:
        return self.raw["news"]

    @property
    def treasure(self) -> Dict[str, Any]:
        return self.raw["treasure"]

    @property
    def tasks(self) -> Dict[str, Any]:
        return self.raw["tasks"]

    @property
    def llm(self) -> Dict[str, Any]:
        return self.raw["llm"]

    @property
    def sandbox(self) -> Dict[str, Any]:
        return self.raw["sandbox"]

    @property
    def judge(self) -> Dict[str, Any]:
        return self.raw["judge"]

    @property
    def agent(self) -> Dict[str, Any]:
        return self.raw["agent"]

    def wave_count(self, robot_type: str, day: int) -> int:
        f = self.robots["wave_formula"][robot_type]
        n = (f["a"] + f["b"] * day) * self.robots["wave_scale"]
        return max(0, int(round(n)))


def load_config(path: str | None = None) -> EngineConfig:
    path = path or repo_path("config", "engine.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return EngineConfig(raw=raw)
