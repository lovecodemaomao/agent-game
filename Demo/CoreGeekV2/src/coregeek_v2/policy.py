"""Small, explicit strategy parameters; no round-number economic phases."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    safety_margin: int = 3
    task_budget: int = 20
    damaged_upgrade_ratio: float = 0.65
    emergency_ratio: float = 0.30
    repair_ratio: float = 0.50
    completion_bonus: float = 1.20
    mine_candidates: int = 5
    stone_safety: int = 1
    weapon_order: tuple = ('rocket', 'rocket', 'railgun')


POLICY = Policy()
