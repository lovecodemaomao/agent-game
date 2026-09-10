# -*- coding: utf-8 -*-
"""对手基线 agent：固定策略卡，10 天不变，非任务期零 LLM。

行为（确定性，无随机源）：
- 白天：工人采最近的矿 -> 背包到阈值去小贩卖 -> 按卡建武器/围墙
- 夜晚：角色回撤到武器旁；火控统一分配（每武器一个操控者打射程内最低血）
- 任务：开拓者白天接任务，任务期以固定 SOP 答案提交
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from agents.framework.llmchannel import LLMChannel
from agents.framework.pathing import greedy_step
from agents.framework.taskagent import TaskAgent
from common import rules as R

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class BaselineAgent:
    daily_quota = 0
    round_budget = 3.5

    def __init__(self, config_path: str = "config/engine.yaml", card_path: str = "config/baselines/turtle_defense.json"):
        with open(card_path, "r", encoding="utf-8") as f:
            self.card = json.load(f)
        self.card_name = self.card.get("name", "unknown")
        self.task_agent = TaskAgent(LLMChannel(daily_quota=0))

    # ==================================================================
    def decide(self, request: Dict[str, Any]) -> Dict[str, Any]:
        phase = R.phase_of(int(request["roundNo"]))
        our = request["teamOur"]
        cmds: Dict[str, Dict[str, Any]] = {}
        roles = {r["id"]: r for r in our["roles"]}
        weapons = [r for r in our["roles"] if r["roleType"] in R.WEAPON_TYPES]
        base = next((r for r in our["roles"] if r["roleType"] == "station"), None)

        # 任务期：任务代理全权接管开拓者（昼夜均推进）
        out_extra = {"prompt": "", "executeCmd": ""}
        if request.get("phaseTask"):
            pv = next((r for r in our["roles"] if r["roleType"] == "pioneer"), None)
            if pv:
                ta = self.task_agent.step(request, pv["id"])
                cmds.update(ta.get("commands", {}))
                out_extra["prompt"] = ta.get("prompt", "")
                out_extra["executeCmd"] = ta.get("executeCmd", "")

        if phase == "day":
            task_active = bool(request.get("phaseTask"))
            for r in our["roles"]:
                if r["roleType"] == "worker":
                    cmd = self._worker_day(request, r, weapons)
                elif r["roleType"] == "pioneer":
                    cmd = None if task_active else self._pioneer_day(request, r)
                else:
                    cmd = None
                if cmd:
                    cmds[str(r["id"])] = cmd
        else:
            # 夜间火控优先（操控者不能另有指令）
            cmds.update(self._night_firecontrol(request, weapons, roles))
            controllers = {int(c["controllerId"]) for c in cmds.values()
                           if c.get("action") == "attack"}
            for r in our["roles"]:
                if r["roleType"] in ("worker", "pioneer") and r["id"] not in controllers \
                        and str(r["id"]) not in cmds:
                    cmd = self._night_guard(request, r, weapons, base)
                    if cmd:
                        cmds[str(r["id"])] = cmd
        out_extra["roleCommandMap"] = cmds
        return out_extra

    # ------------------------------------------------------------------
    def _worker_day(self, request: Dict, wk: Dict, weapons: List[Dict]) -> Optional[Dict[str, Any]]:
        zones = request["mapInfo"]["zones"]
        bp = Counter(wk["backpack"])
        cap = wk.get("backPackCapability", 100)
        # 背包到阈值 -> 卖
        if len(wk["backpack"]) >= min(cap, self.card["sell_backpack_threshold"]):
            vendor = self._find_zone(zones, "vendor")
            if vendor and R.adjacent(wk["pos"], vendor):
                prices = {v["name"]: v["price"] for v in request["vendorShopList"]}
                best = max(set(wk["backpack"]), key=lambda n: prices.get(n, 0))
                return {"action": "sell", "name": best, "num": bp[best]}
            if vendor:
                return self._goto(wk, vendor)
        # 建造优先
        cmd = self._try_build(request, wk, weapons)
        if cmd:
            return cmd
        # 采集：按偏好权重+距离挑最近矿
        mines = [z for z in zones if z["neutralType"] in R.ORES]
        if not mines:
            return self._idle_near(wk, request)
        pref = self.card["mine_preference"]
        nearest = min(mines, key=lambda z: (
            -pref.get(z["neutralType"], 1),
            R.chebyshev(wk["pos"], z["pos"])))
        if R.adjacent(wk["pos"], nearest["pos"]):
            return {"action": "collect", "targetPos": [dict(nearest["pos"])]}
        return self._goto(wk, nearest["pos"])

    def _try_build(self, request: Dict, wk: Dict, weapons: List[Dict]) -> Optional[Dict[str, Any]]:
        base = next((r for r in request["teamOur"]["roles"] if r["roleType"] == "station"), None)
        if not base:
            return None
        bx, by = base["pos"]["x"], base["pos"]["y"]
        zones = request["mapInfo"]["zones"]
        terrain = {(z["pos"]["x"], z["pos"]["y"]) for z in zones}
        my_units = {(r["pos"]["x"], r["pos"]["y"]) for r in request["teamOur"]["roles"]}
        # 武器未满 -> 按卡顺序补蓝区武器
        if len(weapons) < R.MAX_WEAPONS:
            have = Counter(w["roleType"] for w in weapons)
            kind = next((k for k in self.card["weapon_build_order"] if have[k] < 1),
                        self.card["weapon_build_order"][0])
            spot = self._free_build_spot(request, wk, bx, by, 1, 3, terrain | my_units)
            if spot and R.adjacent(wk["pos"], spot):
                return {"action": "build", "name": kind, "targetPos": [spot]}
            if spot:
                return self._goto(wk, spot)
        # 围墙未达标且背包有石头 -> 黄区建墙
        walls = [r for r in request["teamOur"]["roles"] if r["roleType"] == "wall"]
        if len(walls) < self.card["wall_target_per_side"] and "stone" in wk["backpack"]:
            spot = self._free_build_spot(request, wk, bx, by, 4, 6, terrain | my_units)
            if spot and R.adjacent(wk["pos"], spot):
                return {"action": "build", "name": "wall", "targetPos": [spot]}
            if spot:
                return self._goto(wk, spot)
        return None

    def _free_build_spot(self, request: Dict, wk: Dict, bx: int, by: int,
                         lo: int, hi: int, taken: set) -> Optional[Dict[str, int]]:
        w, h = request["mapInfo"]["width"], request["mapInfo"]["height"]
        cands = []
        for dx in range(-hi, hi + 1):
            for dy in range(-hi, hi + 1):
                d = max(abs(dx), abs(dy))
                if not (lo <= d <= hi):
                    continue
                x, y = bx + dx, by + dy
                if 0 <= x < w and 0 <= y < h and (x, y) not in taken:
                    cands.append({"x": x, "y": y})
        if not cands:
            return None
        cands.sort(key=lambda p: (R.chebyshev(wk["pos"], p), p["x"], p["y"]))
        return cands[0]

    # ------------------------------------------------------------------
    def _pioneer_day(self, request: Dict, pv: Dict) -> Optional[Dict[str, Any]]:
        tasks = request["teamOur"].get("playerTasks", [])
        ready = next((t for t in tasks if t.get("isValid")), None)
        if not ready:
            return self._idle_near(pv, request)
        tp = ready["taskPosition"]
        if R.adjacent(pv["pos"], tp):
            return {"action": "acceptTask"}
        return self._goto(pv, tp)

    # ------------------------------------------------------------------
    def _night_guard(self, request: Dict, r: Dict, weapons: List[Dict],
                     base: Optional[Dict]) -> Optional[Dict[str, Any]]:
        robots = request["robot"]["roles"]
        if not robots:
            return None
        if not weapons or not base:
            return self._goto(r, base["pos"]) if base else None
        # 贴靠未配操控者的最近武器
        wp = min(weapons, key=lambda x: R.chebyshev(r["pos"], x["pos"]))
        if not R.adjacent(r["pos"], wp["pos"]):
            return self._goto(r, wp["pos"])
        return None

    def _night_firecontrol(self, request: Dict, weapons: List[Dict],
                           roles: Dict[int, Dict]) -> Dict[str, Dict[str, Any]]:
        cmds = {}
        robots = request["robot"]["roles"]
        if not robots:
            return cmds
        free_chars = [r for r in roles.values()
                      if r["roleType"] in ("worker", "pioneer")]
        for wp in sorted(weapons, key=lambda x: x["id"]):
            if not free_chars:
                break
            ctrl = min(free_chars, key=lambda c: (R.chebyshev(c["pos"], wp["pos"]), c["id"]))
            if not R.adjacent(ctrl["pos"], wp["pos"]):
                continue
            rng = wp.get("attackRange", 3)
            targets = [rb for rb in robots if R.chebyshev(rb["pos"], wp["pos"]) <= rng]
            if not targets:
                continue
            tgt = min(targets, key=lambda rb: rb["health"])
            n_shots = wp["level"] if wp["roleType"] != "railgun" else 1
            tpos = [dict(tgt["pos"]) for _ in range(n_shots)]
            cmds[str(wp["id"])] = {"action": "attack",
                                   "controllerId": str(ctrl["id"]), "targetPos": tpos}
            free_chars.remove(ctrl)
        return cmds

    # ------------------------------------------------------------------
    def _goto(self, r: Dict, goal: Optional[Dict[str, int]]) -> Optional[Dict[str, Any]]:
        if not goal:
            return None
        if R.adjacent(r["pos"], goal):
            return None
        step = greedy_step((r["pos"]["x"], r["pos"]["y"]), (goal["x"], goal["y"]),
                           set(), 41, 32)
        if not step:
            return None
        return {"action": "move", "targetPos": [{"x": step[0], "y": step[1]}]}

    def _idle_near(self, r: Dict, request: Dict) -> Optional[Dict[str, Any]]:
        base = next((x for x in request["teamOur"]["roles"] if x["roleType"] == "station"), None)
        if base and not R.adjacent(r["pos"], base["pos"]):
            return self._goto(r, base["pos"])
        return None

    def _find_zone(self, zones: List[Dict], neutral: str) -> Optional[Dict[str, int]]:
        return next((z["pos"] for z in zones if z["neutralType"] == neutral), None)
