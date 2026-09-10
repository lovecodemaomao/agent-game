# -*- coding: utf-8 -*-
"""我方 agent 确定性调度引擎（任务11.2/11.4）。

- 日常调度零 LLM：采集-贩卖-建造-升级-夜间火控，全部由当前教义参数驱动
- 新闻推理（11.4）：官方消息关键词 -> 囤卖套利与采集偏好调整
- 宝藏求解（11.4）：民间传闻线索正则收敛 -> 三要素齐备高信心召唤
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from agents.competitor.doctrine import DoctrineParams
from agents.framework.pathing import greedy_step
from common import rules as R

ITEM_CN_EN = {v: k for k, v in {
    "AcientTablet": "古符石板", "StarSand": "星辰之沙", "FlameBreath": "烈焰之息",
    "FrostPotion": "寒霜药剂", "ThornAmulet": "荆棘护符", "IronWhistle": "回音铁哨",
}.items()}

# 官方消息关键词 -> 矿种事件（比赛真实新闻关键词可能不同，LLM 日分析可补）
NEWS_KEYWORDS = {
    "塌方": "collapse", "停工": "collapse",
    "富矿脉": "discovery", "供应过剩": "discovery",
    "紧急订单": "surge", "上调": "surge",
}
ORE_KEYWORDS = {"铁矿": "iron", "石矿": "stone", "铜矿": "copper"}


class CompetitorEngine:
    """参数化日常调度。decide() 返回 (roleCommandMap, prompt, executeCmd)。"""

    def __init__(self):
        self.treasure_hypothesis: Dict[str, Any] = {}   # 线索收敛结果
        self.legends_seen: List[str] = []
        self.news_signal: Dict[str, Any] = {}            # 当日新闻事件解读
        self.treasure_done = False

    # ==================================================================
    def decide(self, request: Dict[str, Any], p: DoctrineParams) -> Dict[str, Any]:
        round_no = int(request["roundNo"])
        phase = R.phase_of(round_no)
        our = request["teamOur"]
        cmds: Dict[str, Any] = {}
        weapons = [r for r in our["roles"] if r["roleType"] in R.WEAPON_TYPES]
        base = next((r for r in our["roles"] if r["roleType"] == "station"), None)

        self._read_news(request)

        if phase == "day":
            for r in our["roles"]:
                if r["roleType"] == "worker":
                    c = self._worker_day(request, r, weapons, p)
                elif r["roleType"] == "pioneer":
                    c = self._pioneer_day(request, r, p)
                else:
                    c = None
                if c:
                    cmds[str(r["id"])] = c
        else:
            cmds.update(self._night_firecontrol(request, weapons))
            controllers = {int(c["controllerId"]) for c in cmds.values() if c.get("action") == "attack"}
            for r in our["roles"]:
                if r["roleType"] in ("worker", "pioneer") and r["id"] not in controllers:
                    c = self._night_guard(request, r, weapons, base)
                    if c:
                        cmds[str(r["id"])] = c
        return {"roleCommandMap": cmds, "prompt": "", "executeCmd": ""}

    # ------------------------------------------------------------------
    def _read_news(self, request: Dict[str, Any]) -> None:
        """11.4 新闻推理：官方消息 -> 事件信号（囤卖/避采）；传闻 -> 宝藏线索。"""
        official = request.get("worldNews", {}).get("officialNews", "") or ""
        folk = request.get("worldNews", {}).get("folkLegends", "") or ""
        # 官方事件解读（确定性关键词规则）
        kind = next((k for kw, k in NEWS_KEYWORDS.items() if kw in official), None)
        if kind:
            ore = next((o for kw, o in ORE_KEYWORDS.items() if kw in official), None)
            if ore:
                m = re.search(r"(\d+)天", official)
                dur = int(m.group(1)) if m else 2
                m2 = re.search(r"(\d+(?:\.\d+)?)倍", official)
                mult = float(m2.group(1)) if m2 else 2.0
                self.news_signal = {"kind": kind, "ore": ore, "duration": dur, "mult": mult}
        elif official:
            self.news_signal = {}
        # 传闻线索收敛
        if folk and folk not in self.legends_seen:
            self.legends_seen.append(folk)
            self._update_treasure_hypothesis()

    def _update_treasure_hypothesis(self) -> None:
        full = "\n".join(self.legends_seen)
        h = self.treasure_hypothesis
        m = re.search(r"挑战者营地([东南西北])(\d+)里、([东南西北])(\d+)里", full)
        if m:
            dx = int(m.group(2)) * (1 if m.group(1) == "东" else -1)
            dy = int(m.group(4)) * (1 if m.group(3) == "北" else -1)
            h["pos"] = {"x": 10 + dx, "y": 24 + dy}
        items = [en for cn, en in ITEM_CN_EN.items() if cn in full]
        if items:
            h["items"] = items
        m3 = re.search(r"自第(\d+)天起", full)
        if m3:
            h["window_start"] = int(m3.group(1))
        m4 = re.search(r"至第(\d+)天闭合", full)
        if m4:
            h["window_end"] = int(m4.group(1))

    # ------------------------------------------------------------------
    def _worker_day(self, request: Dict, wk: Dict, weapons: List[Dict],
                    p: DoctrineParams) -> Optional[Dict[str, Any]]:
        zones = request["mapInfo"]["zones"]
        bp = Counter(wk["backpack"])
        signal = self.news_signal
        # 套利：涨价事件在手 -> 立刻抛售该矿种；跌价 -> 停采该矿
        surge_ore = signal.get("ore") if signal.get("kind") in ("collapse", "surge") else None
        crash_ore = signal.get("ore") if signal.get("kind") == "discovery" else None
        if surge_ore and bp.get(surge_ore, 0) > 0:
            vendor = self._find_zone(zones, "vendor")
            if vendor and R.adjacent(wk["pos"], vendor):
                return {"action": "sell", "name": surge_ore, "num": bp[surge_ore]}
            if vendor:
                return self._goto(wk, vendor)
        # 卖包（阈值 + 卖出时机旋钮）
        threshold = p.sell_backpack_threshold
        if p.sell_timing == "hold_on_surge" and not signal:
            threshold = int(threshold * 1.5)
        if len(wk["backpack"]) >= min(wk.get("backPackCapability", 100), threshold):
            vendor = self._find_zone(zones, "vendor")
            if vendor and R.adjacent(wk["pos"], vendor):
                prices = {v["name"]: v["price"] for v in request["vendorShopList"]}
                best = max(set(wk["backpack"]), key=lambda n: prices.get(n, 0))
                return {"action": "sell", "name": best, "num": bp[best]}
            if vendor:
                return self._goto(wk, vendor)
        # 建造
        c = self._try_build(request, wk, weapons, p)
        if c:
            return c
        # 采集（偏好 + 跌价矿种降权）
        mines = [z for z in zones if z["neutralType"] in R.ORES
                 and z["neutralType"] != crash_ore]
        if not mines:
            return self._idle_near(wk, request)
        pref = dict(p.mine_preference)
        nearest = min(mines, key=lambda z: (-pref.get(z["neutralType"], 1),
                                            R.chebyshev(wk["pos"], z["pos"])))
        if R.adjacent(wk["pos"], nearest["pos"]):
            return {"action": "collect", "targetPos": [dict(nearest["pos"])]}
        return self._goto(wk, nearest["pos"])

    def _try_build(self, request: Dict, wk: Dict, weapons: List[Dict],
                   p: DoctrineParams) -> Optional[Dict[str, Any]]:
        base = next((r for r in request["teamOur"]["roles"] if r["roleType"] == "station"), None)
        if not base:
            return None
        bx, by = base["pos"]["x"], base["pos"]["y"]
        terrain = {(z["pos"]["x"], z["pos"]["y"]) for z in request["mapInfo"]["zones"]}
        my_units = {(r["pos"]["x"], r["pos"]["y"]) for r in request["teamOur"]["roles"]}
        # 升级优先级：攒钱买券（旋钮顺序）
        gold = request["teamOur"]["goldNum"]
        c = self._try_upgrade(request, wk, gold, p)
        if c:
            return c
        # 武器未满
        if len(weapons) < R.MAX_WEAPONS:
            have = Counter(w["roleType"] for w in weapons)
            kind = next((k for k in p.weapon_build_order if have[k] < 1),
                        p.weapon_build_order[0])
            spot = self._free_spot(request, wk, bx, by, 1, 3, terrain | my_units)
            if spot and R.adjacent(wk["pos"], spot):
                return {"action": "build", "name": kind, "targetPos": [spot]}
            if spot:
                return self._goto(wk, spot)
        # 围墙（密度旋钮 -> 目标数量）
        walls = [r for r in request["teamOur"]["roles"] if r["roleType"] == "wall"]
        wall_target = int(p.wall_target_per_side * p.wall_density)
        if len(walls) < wall_target and "stone" in wk["backpack"]:
            spot = self._free_spot(request, wk, bx, by, 4, 6, terrain | my_units)
            if spot and R.adjacent(wk["pos"], spot):
                return {"action": "build", "name": "wall", "targetPos": [spot]}
            if spot:
                return self._goto(wk, spot)
        return None

    def _try_upgrade(self, request: Dict, wk: Dict, gold: int,
                     p: DoctrineParams) -> Optional[Dict[str, Any]]:
        """按优先级买升级券并在目标建筑旁使用（简化：先买券，下一步贴靠使用）。"""
        our = request["teamOur"]["roles"]
        prices = {v["name"]: v["price"] for v in request["weaponShopList"]}
        shop = self._find_zone(request["mapInfo"]["zones"], "weaponShop")
        plan = {
            "base": ("StationUpgradeVoucher1", "station", 100),
            "wall": ("WallUpgradeVoucher1", "wall", 20),
            "weapon": ("WeaponUpgradeVoucher1", "weapon", 100),
        }
        for key in p.upgrade_priority:
            voucher, btype, _ = plan[key]
            if gold < prices.get(voucher, 9999) + 30:
                continue
            targets = [r for r in our if r["roleType"] == btype and r.get("level", 1) == 1]
            if not targets:
                continue
            if voucher in wk["backpack"]:
                tgt = min(targets, key=lambda t: R.chebyshev(wk["pos"], t["pos"]))
                if R.adjacent(wk["pos"], tgt["pos"]):
                    return {"action": "use", "name": voucher, "targetPos": [dict(tgt["pos"])]}
                return self._goto(wk, tgt["pos"])
            if shop and R.adjacent(wk["pos"], shop):
                return {"action": "buy", "name": voucher, "num": 1}
            if shop:
                return self._goto(wk, shop)
        return None

    def _free_spot(self, request: Dict, wk: Dict, bx: int, by: int, lo: int, hi: int,
                   taken: set) -> Optional[Dict[str, int]]:
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
        cands.sort(key=lambda q: (R.chebyshev(wk["pos"], q), q["x"], q["y"]))
        return cands[0]

    # ------------------------------------------------------------------
    def _pioneer_day(self, request: Dict, pv: Dict, p: DoctrineParams) -> Optional[Dict[str, Any]]:
        """任务节奏旋钮：task_tempo 高 -> 优先任务；宝藏假说齐备 -> 召唤优先。"""
        if self.treasure_done:
            return self._idle_near(pv, request)
        h = self.treasure_hypothesis
        day = R.day_of(int(request["roundNo"]))
        if h.get("pos") and h.get("items") and h.get("window_start"):
            return self._treasure_run(request, pv, h, day)
        if request.get("phaseTask"):
            return None  # 任务期由 TaskAgent 接管
        tasks = request["teamOur"].get("playerTasks", [])
        ready = next((t for t in tasks if t.get("isValid")), None)
        if ready and p.task_tempo > 0.3:
            tp = ready["taskPosition"]
            if R.adjacent(pv["pos"], tp):
                return {"action": "acceptTask"}
            return self._goto(pv, tp)
        # 低任务节奏：买宝藏祭品备用
        return self._buy_items(request, pv, h, p)

    def _treasure_run(self, request: Dict, pv: Dict, h: Dict, day: int) -> Optional[Dict[str, Any]]:
        """三要素齐备 -> 高信心召唤流程（11.4）。"""
        pos, items = h["pos"], h["items"]
        w_start = h.get("window_start", 1)
        if day < w_start - 1:
            # 窗口未开：先备齐祭品
            return self._buy_items(request, pv, h, None) or self._idle_near(pv, request)
        missing = [i for i in items if i not in pv["backpack"]]
        if missing:
            return self._buy_items(request, pv, {"items": missing}, None) or self._idle_near(pv, request)
        # 祭品齐 -> 前往召唤
        if R.chebyshev(pv["pos"], pos) <= 1:
            self.treasure_done = True  # 无论成败只试一次（失败已消耗）
            return {"action": "summonTreasure", "targetPos": [dict(pos)], "item": list(items)}
        return self._goto(pv, pos)

    def _buy_items(self, request: Dict, pv: Dict, h: Optional[Dict],
                   p: Optional[DoctrineParams]) -> Optional[Dict[str, Any]]:
        need = (h or {}).get("items") or []
        if not need:
            return None
        shop = self._find_zone(request["mapInfo"]["zones"], "weaponShop")
        if not shop:
            return None
        missing = [i for i in need if i not in pv["backpack"]]
        prices = {v["name"]: v["price"] for v in request["weaponShopList"]}
        gold = request["teamOur"]["goldNum"]
        if shop and R.adjacent(pv["pos"], shop):
            buy = next((i for i in missing if gold >= prices.get(i, 999)), None)
            if buy:
                return {"action": "buy", "name": buy, "num": 1}
            return None
        return self._goto(pv, shop)

    # ------------------------------------------------------------------
    def _night_firecontrol(self, request: Dict, weapons: List[Dict]) -> Dict[str, Dict[str, Any]]:
        cmds: Dict[str, Dict[str, Any]] = {}
        robots = request.get("robot", {}).get("roles", []) or []
        if not robots:
            return cmds
        our = request["teamOur"]["roles"]
        # 保守生存：基地血量低于阈值 -> 优先杀最近机器人（简化统一逻辑）
        free = [r for r in our if r["roleType"] in ("worker", "pioneer")]
        for wp in sorted(weapons, key=lambda x: x["id"]):
            if not free:
                break
            ctrl = min(free, key=lambda c: (R.chebyshev(c["pos"], wp["pos"]), c["id"]))
            if not R.adjacent(ctrl["pos"], wp["pos"]):
                continue
            rng = wp.get("attackRange", 3)
            targets = [rb for rb in robots if R.chebyshev(rb["pos"], wp["pos"]) <= rng]
            if not targets:
                continue
            tgt = min(targets, key=lambda rb: rb["health"])
            n_shots = wp["level"] if wp["roleType"] != "railgun" else 1
            cmds[str(wp["id"])] = {"action": "attack", "controllerId": str(ctrl["id"]),
                                   "targetPos": [dict(tgt["pos"]) for _ in range(n_shots)]}
            free.remove(ctrl)
        return cmds

    def _night_guard(self, request: Dict, r: Dict, weapons: List[Dict],
                     base: Optional[Dict]) -> Optional[Dict[str, Any]]:
        robots = request.get("robot", {}).get("roles", []) or []
        if not robots:
            return None
        if not weapons or not base:
            return self._goto(r, base["pos"]) if base else None
        wp = min(weapons, key=lambda x: R.chebyshev(r["pos"], x["pos"]))
        if not R.adjacent(r["pos"], wp["pos"]):
            return self._goto(r, wp["pos"])
        return None

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
