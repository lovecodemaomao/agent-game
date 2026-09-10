# -*- coding: utf-8 -*-
"""教义×旋钮参数（任务11.2）：离散教义预设卡 + 连续旋钮 + 硬夹紧 + 热替换。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List

from sim.engine.config import repo_path

# 每个旋钮的硬取值域（LLM 幻觉输出最坏情况 = 被夹紧，不可能自杀）
KNOB_RANGES: Dict[str, tuple] = {
    "harass_budget": (0.0, 0.5),
    "wall_density": (0.5, 2.0),
    "task_tempo": (0.0, 1.0),
    "sell_backpack_threshold": (4, 30),
    "wall_target_per_side": (2, 12),
    "base_hp_warn_ratio": (0.2, 0.8),
}
# 列表/字典旋钮仅做合法性校验
KNOB_CHOICES: Dict[str, tuple] = {
    "sell_timing": ("immediate", "hold_on_surge"),
    "upgrade_priority": (("base", "wall", "weapon"),),
}


@dataclass
class DoctrineParams:
    name: str = "balanced"
    description: str = ""
    harass_budget: float = 0.0          # 盈余金币投召唤令的比例
    wall_density: float = 1.0           # 围墙密度系数
    weapon_build_order: List[str] = field(default_factory=lambda: ["gatling", "gatling", "railgun"])
    task_tempo: float = 0.7             # 开拓者任务优先级
    mine_preference: Dict[str, int] = field(default_factory=lambda: {"stone": 1, "iron": 2, "copper": 3})
    upgrade_priority: List[str] = field(default_factory=lambda: ["base", "wall", "weapon"])
    sell_timing: str = "immediate"
    sell_backpack_threshold: int = 12
    wall_target_per_side: int = 6
    base_hp_warn_ratio: float = 0.5

    # ------------------------------------------------------------------
    def clamp(self) -> "DoctrineParams":
        for f in fields(self):
            rng = KNOB_RANGES.get(f.name)
            if rng:
                v = getattr(self, f.name)
                setattr(self, f.name, max(rng[0], min(rng[1], v)))
        return self

    def apply_overrides(self, overrides: Dict[str, Any]) -> "DoctrineParams":
        """LLM 输出的旋钮微调：白名单字段 + 类型校验 + 夹紧。"""
        valid = {f.name for f in fields(self)} - {"name", "description"}
        for k, v in (overrides or {}).items():
            if k not in valid:
                continue
            cur = getattr(self, k)
            try:
                if isinstance(cur, float):
                    setattr(self, k, float(v))
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    setattr(self, k, int(round(float(v))))
                elif isinstance(cur, str):
                    if v in KNOB_CHOICES.get(k, (v,)):
                        setattr(self, k, v)
                elif isinstance(cur, list) and isinstance(v, list):
                    setattr(self, k, v)
                elif isinstance(cur, dict) and isinstance(v, dict):
                    getattr(self, k).update({kk: int(vv) for kk, vv in v.items()
                                             if kk in cur})
            except (TypeError, ValueError):
                continue  # 非法类型直接忽略
        return self.clamp()

    def copy(self) -> "DoctrineParams":
        import copy
        return copy.deepcopy(self)


DOCTRINE_DIR = repo_path("config", "doctrines")


def load_doctrine(name: str) -> DoctrineParams:
    fp = os.path.join(DOCTRINE_DIR, f"{name}.json")
    with open(fp, "r", encoding="utf-8") as f:
        card = json.load(f)
    p = DoctrineParams()
    p.name = card.get("name", name)
    p.description = card.get("description", "")
    return p.apply_overrides(card)


def doctrine_index() -> List[Dict[str, str]]:
    """教义索引（给 LLM 的一屏卡片：名称+一句话描述）。"""
    out = []
    for fn in sorted(os.listdir(DOCTRINE_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(DOCTRINE_DIR, fn), "r", encoding="utf-8") as f:
                card = json.load(f)
            out.append({"name": card.get("name", fn[:-5]),
                        "description": card.get("description", "")})
    return out
