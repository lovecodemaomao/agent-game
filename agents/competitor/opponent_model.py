# -*- coding: utf-8 -*-
"""OpponentModel（任务11.1）：每回合从全局信息通道 diff 收集对手确定性动作。

通道：敌方基地/围墙（全局可见）、机器人（全局可见）、视野内敌方单位、矿价货架。
输出：动作事件流（逐回合）+ 滚动特征（围墙增速/火力指纹/骚扰强度/任务活动）。
"""
from __future__ import annotations

from collections import Counter, deque
from typing import Any, Dict, List, Optional, Tuple

from common import rules as R

HISTORY = 20  # 滚动窗口长度


class OpponentModel:
    def __init__(self, team: str = "challenger"):
        self.team = team
        self.enemy = "defender" if team == "challenger" else "challenger"
        self.round_no = 0
        self.day = 1
        # 建筑快照（全局可见部分）
        self._prev_buildings: Dict[int, Dict] = {}
        # 机器人快照（血量）
        self._prev_robots: Dict[int, int] = {}
        self._robot_seen_round: Dict[int, int] = {}   # 首次出现回合
        # 事件流与滚动序列
        self.events: deque = deque(maxlen=300)
        self.wall_counts: deque = deque(maxlen=HISTORY)       # (round, count)
        self.weapon_levels: Dict[int, int] = {}               # 可见武器等级
        self.damage_log: deque = deque(maxlen=HISTORY)        # (round, [每跳伤害])
        self.kill_events_per_day: Dict[int, int] = {}
        self.harass_events: List[Dict] = []                    # 超基线波次/targetTeam证据
        self.task_sightings: deque = deque(maxlen=HISTORY)     # 视野内敌开拓者贴任务点
        self.last_features: Dict[str, Any] = {}

    # ==================================================================
    def update(self, request: Dict[str, Any]) -> None:
        self.round_no = int(request["roundNo"])
        self.day = R.day_of(self.round_no)
        enemy_roles = request.get("teamEnemy", {}).get("roles", []) or []
        robots = request.get("robot", {}).get("roles", []) or []

        self._diff_buildings(enemy_roles)
        self._diff_robots(robots, self.day)
        self._watch_tasks(request, enemy_roles)

        walls = sum(1 for b in self._prev_buildings.values() if b["roleType"] == "wall")
        self.wall_counts.append((self.round_no, walls))

    # ------------------------------------------------------------------
    def _diff_buildings(self, enemy_roles: List[Dict]) -> None:
        cur: Dict[int, Dict] = {}
        for r in enemy_roles:
            if r["roleType"] in ("station", "wall", "gatling", "railgun", "rocket"):
                cur[r["id"]] = r
        # 新增
        for rid, r in cur.items():
            if rid not in self._prev_buildings:
                self.events.append({"round": self.round_no, "kind": "enemy_build",
                                    "type": r["roleType"], "pos": r["pos"]})
            else:
                prev = self._prev_buildings[rid]
                if r.get("level", 1) > prev.get("level", 1):
                    self.events.append({"round": self.round_no, "kind": "enemy_upgrade",
                                        "type": r["roleType"], "level": r.get("level")})
                if r["health"] < prev["health"] - 0:
                    self.events.append({"round": self.round_no, "kind": "enemy_building_damaged",
                                        "type": r["roleType"], "delta": prev["health"] - r["health"]})
            if r["roleType"] in R.WEAPON_TYPES:
                self.weapon_levels[rid] = r.get("level", 1)
        # 消失（被拆）
        for rid in self._prev_buildings:
            if rid not in cur:
                self.events.append({"round": self.round_no, "kind": "enemy_building_gone",
                                    "type": self._prev_buildings[rid]["roleType"]})
        self._prev_buildings = cur

    # ------------------------------------------------------------------
    def _diff_robots(self, robots: List[Dict], day: int) -> None:
        cur_hp: Dict[int, int] = {}
        round_damage: List[int] = []
        kills = 0
        for rb in robots:
            rid = rb["id"]
            cur_hp[rid] = rb["health"]
            if rid not in self._prev_robots:
                self._robot_seen_round[rid] = self.round_no
                # 夜间第1回合后新出现 -> 召唤令骚扰证据
                r_in_day = (self.round_no - 1) % R.ROUNDS_PER_DAY
                if r_in_day > R.DAY_ROUNDS:
                    ev = {"round": self.round_no, "kind": "enemy_harass",
                          "robot": rb["roleType"], "target": rb.get("targetTeam")}
                    self.events.append(ev)
                    self.harass_events.append(ev)
            else:
                drop = self._prev_robots[rid] - rb["health"]
                if drop > 0:
                    round_damage.append(drop)
            if rb["health"] <= 0:
                kills += 1
        # 消失的机器人 = 被击杀（或清晨清除）
        gone = [rid for rid in self._prev_robots if rid not in cur_hp]
        if gone and R.phase_of(self.round_no) != "day":
            self.kill_events_per_day[day] = self.kill_events_per_day.get(day, 0) + len(gone)
            self.events.append({"round": self.round_no, "kind": "enemy_kills",
                                "count": len(gone)})
        if round_damage:
            self.damage_log.append((self.round_no, round_damage))
        self._prev_robots = cur_hp

    # ------------------------------------------------------------------
    def _watch_tasks(self, request: Dict, enemy_roles: List[Dict]) -> None:
        """视野内敌开拓者贴我方任务点附近 -> 对手任务活动证据。"""
        our_tasks = request.get("teamOur", {}).get("playerTasks", []) or []
        for r in enemy_roles:
            if r["roleType"] != "pioneer":
                continue
            for tp in our_tasks:
                if R.chebyshev(r["pos"], tp["taskPosition"]) <= 2:
                    self.task_sightings.append({"round": self.round_no, "at": tp["taskPosition"]})
                    self.events.append({"round": self.round_no, "kind": "enemy_task_activity"})

    # ==================================================================
    def features(self) -> Dict[str, Any]:
        """给每日 LLM 分析的仪表盘特征。"""
        walls_now = self.wall_counts[-1][1] if self.wall_counts else 0
        walls_13 = self.wall_counts[0][1] if self.wall_counts else 0
        n = max(1, len(self.wall_counts))
        growth = (walls_now - walls_13) / n
        # 火力指纹：每跳伤害直方图 -> 推断武器构成
        hist: Counter = Counter()
        for _, dmg_list in self.damage_log:
            for d in dmg_list:
                hist[min(d // 10, 8)] += 1
        kills_recent = sum(self.kill_events_per_day.values())
        feats = {
            "round": self.round_no,
            "enemy_wall_count": walls_now,
            "enemy_wall_growth_per_round": round(growth, 2),
            "enemy_visible_weapons": {str(k): v for k, v in self.weapon_levels.items()},
            "damage_histogram_x10": dict(hist),
            "est_dps_rounds": sum(sum(d) for _, d in self.damage_log),
            "harass_events": len(self.harass_events),
            "harass_detail": self.harass_events[-3:],
            "enemy_kill_events_total": kills_recent,
            "enemy_task_activity_sightings": len(self.task_sightings),
            "recent_events": list(self.events)[-8:],
        }
        self.last_features = feats
        return feats

    def dashboard_text(self) -> str:
        import json
        return json.dumps(self.features(), ensure_ascii=False)
