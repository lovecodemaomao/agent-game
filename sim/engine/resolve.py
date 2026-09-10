# -*- coding: utf-8 -*-
"""回合结算器（任务4.3-4.7）。

结算顺序（design D2）：
  1. 物品效果（眩晕/炸弹，优先级 > 机器人移动）
  2. 角色行动（除 move 外当场结算；move 先登记意图）
  3. 武器攻击（伤害延迟到回合末统一结算）
  4. 角色移动（三情形碰撞；机器人按移动前位置视作阻挡物）
  5. 机器人移动（撞上阻挡者即攻击，伤害同样延迟）
  6. 伤害统一结算 -> 死亡处理
  7. 回合收尾（冷却/复活/波次/矿区刷新/积分/胜负）
"""
from __future__ import annotations

import random
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from common import rules as R
from sim.engine.config import EngineConfig
from sim.engine.state import Mine, TaskPointState, Unit, WorldState

DIRS8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]


class Resolver:
    def __init__(self, cfg: EngineConfig, seed: int = 42):
        self.cfg = cfg
        self.rng = random.Random(seed + 777)

    # ==================================================================
    def step(self, w: WorldState, commands: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
        """commands: team -> {roleId(str|int): command}（已经过出站校验）。"""
        norm: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for team, cmap in commands.items():
            norm[team] = {}
            for k, c in (cmap or {}).items():
                try:
                    norm[team][int(k)] = c
                except (TypeError, ValueError):
                    continue
        commands = norm
        w.round_no += 1
        w._fired_weapons = set()
        w.events = []
        w.last_results = {t: {} for t in ("challenger", "defender")}
        w.last_summon_result = {}
        day = R.day_of(w.round_no)

        self._day_boundaries(w, day)

        # 1) 物品效果（眩晕/炸弹）与其他 use
        for team in ("challenger", "defender"):
            for rid, cmd in commands.get(team, {}).items():
                if cmd.get("action") == "use" and cmd.get("name") in ("DizzyWeapon", "Bomb"):
                    self._apply_combat_item(w, team, rid, cmd)

        # 2) 角色行动
        move_intents: Dict[int, Dict[str, int]] = {}
        for team in ("challenger", "defender"):
            for rid, cmd in commands.get(team, {}).items():
                u = w.units.get(rid)
                if not u or not u.alive or u.team != team or u.role_type not in ("pioneer", "worker"):
                    continue
                self._character_action(w, team, u, cmd, move_intents)

        # 3) 武器攻击（夜晚）
        if R.phase_of(w.round_no) == "night":
            for team in ("challenger", "defender"):
                for rid, cmd in commands.get(team, {}).items():
                    if cmd.get("action") == "attack":
                        u = w.units.get(rid)
                        if u and u.alive and u.team == team and u.role_type in R.WEAPON_TYPES:
                            self._weapon_attack(w, team, u, cmd)

        # 4) 角色移动（碰撞三情形）
        self._resolve_character_moves(w, move_intents)

        # 5) 机器人移动与攻击（夜晚）
        if R.phase_of(w.round_no) == "night":
            self._robots_act(w)

        # 6) 伤害统一结算
        self._settle_damage(w)

        # 7) 收尾
        self._end_of_round(w, day)

    # ------------------------------------------------------------------
    def _day_boundaries(self, w: WorldState, day: int) -> None:
        if R.is_night_first_round(w.round_no):
            self._spawn_robots(w, day)
        if R.is_day_first_round(w.round_no):
            # 清除残余机器人 + 复活窗口开启 + 每日配额相关状态
            for u in w.alive_robots():
                u.alive = False
            if w.round_no > 1:
                for t, ts in w.teams.items():
                    ts.summons_used_today = 0

    # ------------------------------------------------------------------
    def _character_action(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any],
                          move_intents: Dict[int, Dict[str, int]]) -> None:
        action = cmd.get("action")
        legal = True

        if action == "move":
            dst = cmd["targetPos"][0]
            if R.chebyshev(u.pos, dst) != 1:
                legal = False  # 每回合只能移动一格
            else:
                move_intents[u.id] = dst  # 合法性暂记，碰撞结果在移动阶段覆盖

        elif action == "collect":
            legal = self._do_collect(w, team, u, cmd)
        elif action == "sell":
            legal = self._do_sell(w, team, u, cmd)
        elif action == "buy":
            legal = self._do_buy(w, team, u, cmd)
        elif action == "build":
            legal = self._do_build(w, team, u, cmd)
        elif action == "remove":
            legal = self._do_remove(w, team, u, cmd)
        elif action == "use":
            legal = self._do_use(w, team, u, cmd)
        elif action == "drop":
            legal = self._do_drop(w, team, u, cmd)
        elif action == "acceptTask":
            legal = self._do_accept_task(w, team, u)
        elif action == "submitAnswer":
            legal = self._do_submit_answer(w, team, u, cmd)
        elif action == "summonTreasure":
            legal = self._do_summon(w, team, u, cmd)
        else:
            legal = False

        w.last_results[team][u.id] = legal

    # ------------------------------------------------------------------
    def _do_collect(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        tp = cmd["targetPos"][0]
        mine = next((m for m in w.mines if m.pos == tp and m.remaining > 0
                     and R.adjacent(u.pos, tp)), None)
        if not mine or w.news_mine_blocked.get(mine.kind, 0) > 0:
            return False
        # 不足平分时每工人仍得1（任务书4.1）；常规扣减
        mine.remaining -= 1
        u.backpack.append(mine.kind)
        w.emit("collect", team=team, unit=u.id, mine=mine.kind)
        return True

    def _do_sell(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        if not R.adjacent(u.pos, self._zone_pos(w, "vendor")):
            return False
        name, num = cmd["name"], int(cmd.get("num", 1))
        if u.backpack.count(name) < num:
            return False
        price = w.news_prices.get(name, self.cfg.economy["base_prices"][name])
        for _ in range(num):
            u.backpack.remove(name)
        w.teams[team].gold += price * num
        w.emit("sell", team=team, unit=u.id, item=name, num=num, gold=price * num)
        return True

    def _do_buy(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        if not R.adjacent(u.pos, self._zone_pos(w, "weaponShop")):
            return False
        name, num = cmd["name"], int(cmd.get("num", 1))
        price = R.WEAPON_SHOP_ITEMS.get(name)
        if price is None:
            return False
        ts = w.teams[team]
        if name in R.SUMMON_ORDERS:
            if ts.summons_used_today + num > R.ROBOT_DAILY_SUMMON_LIMIT:
                return False
        if ts.gold < price * num:
            return False
        if len(u.backpack) + num > R.BACKPACK_CAP[u.role_type]:
            return False
        ts.gold -= price * num
        if name in R.SUMMON_ORDERS:
            ts.summons_used_today += num
        for _ in range(num):
            u.backpack.append(name)
        w.emit("buy", team=team, unit=u.id, item=name, num=num)
        return True

    def _do_build(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        name, tp = cmd["name"], cmd["targetPos"][0]
        if not R.adjacent(u.pos, tp):
            return False
        occ = w.occupied()
        cell = (tp["x"], tp["y"])
        # 覆盖规则：新建武器可覆盖已有武器（任务书4.5.1）
        overwrite = None
        if name in R.WEAPON_TYPES and cell in occ:
            occupant = w.units.get(occ[cell])
            if occupant and occupant.alive and occupant.team == team \
                    and occupant.role_type in R.WEAPON_TYPES:
                overwrite = occupant
            else:
                return False
        elif cell in occ:
            return False
        d = w.zone_distance(tp, team)
        if name == "wall":
            if not (4 <= d <= self.cfg.raw["map"]["wall_zone"][1]):
                return False
            if u.backpack.count("stone") < 1:
                return False
            u.backpack.remove("stone")
            wid = w.wall_seq[team]
            w.wall_seq[team] += 1
            w.units[wid] = Unit(wid, team, tp, "wall", hp=R.ROLE_LEVEL_HP["wall"][1])
            w.emit("build", team=team, unit=u.id, kind="wall", pos=tp)
            return True
        if name not in R.WEAPON_TYPES:
            return False
        if not (self.cfg.raw["map"]["weapon_zone"][0] <= d <= self.cfg.raw["map"]["weapon_zone"][1]):
            return False
        ts = w.teams[team]
        if len(w.team_weapons(team)) >= R.MAX_WEAPONS and not overwrite:
            return False
        if ts.gold < R.WEAPON_BUILD_COST:
            return False
        ts.gold -= R.WEAPON_BUILD_COST
        if overwrite:
            overwrite.alive = False
            wid = overwrite.id
        else:
            ids = R.TEAM_IDS[team][name]
            wid = next(i for i in ids if i not in w.units or not w.units[i].alive)
        w.units[wid] = Unit(wid, team, tp, name, hp=R.ROLE_LEVEL_HP[name][1])
        w.emit("build", team=team, unit=u.id, kind=name, pos=tp, overwrite=bool(overwrite))
        return True

    def _do_remove(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        tp = cmd["targetPos"][0]
        wall = next((x for x in w.units.values() if x.alive and x.team == team
                     and x.role_type == "wall"
                     and x.pos["x"] == tp["x"] and x.pos["y"] == tp["y"]
                     and R.adjacent(u.pos, tp)), None)
        if not wall:
            return False
        wall.alive = False
        del w.units[wall.id]
        w.emit("remove", team=team, unit=u.id, pos=tp)
        return True

    def _do_use(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        name = cmd["name"]
        if name not in u.backpack:
            return False
        if name in R.UPGRADE_VOUCHERS:
            return self._use_voucher(w, team, u, cmd, name)
        if name == "Medicine":
            u.hp = R.ROLE_HP[u.role_type]
        elif name == "WallFixer":
            tp = cmd["targetPos"][0]
            wall = next((x for x in w.units.values() if x.alive and x.team == team
                         and x.role_type == "wall"
                         and x.pos["x"] == tp["x"] and x.pos["y"] == tp["y"]
                         and R.adjacent(u.pos, tp)), None)
            if not wall:
                return False
            wall.hp = wall.max_hp()
        else:  # 战斗物品在步骤1处理；其他物品（召唤令）此处仅消耗
            if name not in R.SUMMON_ORDERS and name not in ("DizzyWeapon", "Bomb"):
                return False
            if name in R.SUMMON_ORDERS:
                # 召唤令在购买方使用时挂到对方下夜
                enemy = "defender" if team == "challenger" else "challenger"
                w.teams[enemy].summon_orders_pending.append(R.SUMMON_ORDERS[name])
                w.emit("summon_order", team=team, robot=R.SUMMON_ORDERS[name])
        u.backpack.remove(name)
        w.emit("use", team=team, unit=u.id, item=name)
        return True

    def _use_voucher(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any], name: str) -> bool:
        kind, lv_from, lv_to = R.UPGRADE_VOUCHERS[name]
        tp = cmd["targetPos"][0]
        target = next((x for x in w.units.values() if x.alive and x.team == team
                       and x.pos["x"] == tp["x"] and x.pos["y"] == tp["y"]
                       and R.adjacent(u.pos, tp)), None)
        if not target:
            return False
        is_weapon = target.role_type in R.WEAPON_TYPES
        if kind == "weapon" and not is_weapon:
            return False
        if kind == "wall" and target.role_type != "wall":
            return False
        if kind == "station" and target.role_type != "station":
            return False
        if target.level != lv_from or target.level >= 3:
            return False  # 不生效不消耗（任务书4.6.3）
        target.level = lv_to
        target.hp = target.max_hp()
        u.backpack.remove(name)
        w.emit("upgrade", team=team, unit=target.id, level=lv_to)
        return True

    def _do_drop(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        name = cmd["name"]
        if name not in u.backpack:
            return False
        u.backpack.remove(name)
        return True

    # -- 任务/宝藏（组8/10完善，此处提供状态机骨架）--
    def _do_accept_task(self, w: WorldState, team: str, u: Unit) -> bool:
        tp = self._task_point_at(w, team, u.pos)
        if not tp or tp.remaining_total <= 0 or tp.cold_down > 0 or tp.active_task:
            return False
        tp.remaining_total -= 1
        # 天气查询任务：城市与期望答案确定性生成（与 mock API 共用天气表）
        from sim.sandbox.mockapi import weather_of
        cities = self.cfg.tasks.get("weather_cities", ["北京", "上海"])
        city = cities[tp.remaining_total % len(cities)]
        expected = weather_of(city)
        api_url = getattr(self, "task_api_url", "") or "http://127.0.0.1:0"
        desc = (f"【自进化任务】请查询{city}今天的天气并提交答案。\n"
                f"第三方天气API文档：GET {api_url}/weather?city=<城市名>\n"
                f"返回 JSON：{{\"city\": \"...\", \"weather\": \"...\"}}\n"
                f"完成后调用 submitAnswer，答案中需包含该城市的天气现象。")
        tp.active_task = {
            "start_round": w.round_no, "timeout_rounds": tp.timeout_rounds,
            "best_answer": "", "best_rate": 0.0,
            "desc": desc, "city": city, "expected": expected,
        }
        w.phase_task[team] = desc
        w.emit("task_accept", team=team, unit=u.id, point=tp.point_id, city=city)
        return True

    def _do_submit_answer(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        tp = next((t for t in w.task_points[team] if t.active_task), None)
        if not tp:
            return False
        at = tp.active_task
        expected = at.get("expected", "")
        if expected:
            rate = 1.0 if expected in cmd["taskAnswer"] else 0.3
        else:
            rate = 1.0 if "正确" in cmd["taskAnswer"] else 0.3
        if rate > at["best_rate"]:
            at["best_rate"], at["best_answer"] = rate, cmd["taskAnswer"]
        if rate >= 1.0:
            self._finish_task(w, team, tp, completed=True)
        return True

    def _finish_task(self, w: WorldState, team: str, tp: TaskPointState, completed: bool) -> None:
        at = tp.active_task
        ts = w.teams[team]
        if completed:
            speed = 5.0 * tp.timeout_rounds / max(1, w.round_no - at["start_round"])
            ts.score_task += tp.score_reward + speed
            ts.gold += int(tp.gold_reward * 1.0)
        else:
            ts.score_task += tp.score_reward * at["best_rate"]
            ts.gold += int(tp.gold_reward * at["best_rate"])
        tp.active_task = None
        tp.cold_down = self.cfg.tasks["cooldown_rounds"]
        if not hasattr(w, "_task_finished_this_round"):
            w._task_finished_this_round = set()
        w._task_finished_this_round.add((team, tp.point_id))
        w.phase_task[team] = ""
        w.emit("task_finish", team=team, point=tp.point_id, completed=completed)

    def _do_summon(self, w: WorldState, team: str, u: Unit, cmd: Dict[str, Any]) -> bool:
        tp = cmd["targetPos"][0]
        items = cmd["item"]
        if not R.adjacent(u.pos, tp):
            return False
        for it in items:
            if it not in u.backpack:
                return False
        need = w.treasure.get("items_needed", [])
        code = 2
        if (not w.treasure_taken and w.treasure.get("pos") == tp
                and sorted(items) == sorted(need)
                and w.treasure.get("window", (0, 0))[0] <= R.day_of(w.round_no) <= w.treasure.get("window", (0, 0))[1]):
            code = 1
            w.treasure_taken = True
            ts = w.teams[team]
            ts.score_task += w.treasure.get("reward_score", 100)
            ts.gold += w.treasure.get("reward_gold", 200)
            w.emit("treasure_opened", team=team)
        elif w.treasure_taken and w.treasure.get("pos") == tp and sorted(items) == sorted(need):
            code = 4
        elif sorted(items) != sorted(need):
            code = 3
        for it in items:  # 合法动作必消耗祭品
            u.backpack.remove(it)
        w.last_summon_result[team] = code
        w.emit("summon", team=team, code=code)
        return True

    # ------------------------------------------------------------------
    def _apply_combat_item(self, w: WorldState, team: str, rid: int, cmd: Dict[str, Any]) -> bool:
        u = w.units.get(rid)
        name = cmd["name"]
        if not u or not u.alive or name not in u.backpack:
            return False
        tp = cmd["targetPos"][0]
        if name == "DizzyWeapon":
            for r in w.alive_robots():
                if R.chebyshev(r.pos, tp) <= 1:
                    r.abnormal = "dizzy"
                    r._dizzy_left = 5
        elif name == "Bomb":
            for r in w.alive_robots():
                if R.chebyshev(r.pos, tp) <= 1:
                    w.pending_damage[r.id] = w.pending_damage.get(r.id, 0) + 100
        u.backpack.remove(name)
        w.emit("combat_item", team=team, item=name, pos=tp)
        return True

    # ------------------------------------------------------------------
    def _weapon_attack(self, w: WorldState, team: str, wp: Unit, cmd: Dict[str, Any]) -> None:
        controller = w.units.get(int(cmd.get("controllerId", 0)))
        if not controller or not controller.alive or controller.team != team:
            w.last_results[team][wp.id] = False
            return
        if not R.adjacent(controller.pos, wp.pos):
            w.last_results[team][wp.id] = False
            return
        if wp.cooldown > 0:
            w.last_results[team][wp.id] = False
            return
        targets = cmd["targetPos"]
        rng = wp.attack_range()
        for t in targets:
            if R.chebyshev(wp.pos, t) > rng:
                w.last_results[team][wp.id] = False
                return
        legal = True
        if wp.role_type == "gatling":
            for t in targets:
                self._gatling_bullet(w, team, wp, t)
        elif wp.role_type == "railgun":
            self._railgun_beam(w, team, wp, targets[0])
        elif wp.role_type == "rocket":
            if wp.level == 1:
                self._rocket_hit(w, team, wp, targets[0])
            else:
                for t in targets:
                    self._rocket_hit(w, team, wp, t)
            wp.cooldown = 3
            w._fired_weapons.add(wp.id)  # 发射回合不递减冷却
        w.last_results[team][wp.id] = legal
        w.emit("attack", team=team, weapon=wp.id, targets=targets)

    def _line_cells(self, a: Dict[str, int], b: Dict[str, int]) -> List[Dict[str, int]]:
        """攻击路径：两坐标中心连线的整数栅格（简化 Bresenham）。"""
        cells = []
        x0, y0, x1, y1 = a["x"], a["y"], b["x"], b["y"]
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x1 >= x0 else -1
        sy = 1 if y1 >= y0 else -1
        err = dx - dy
        x, y = x0, y0
        while True:
            cells.append({"x": x, "y": y})
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy
        return cells

    def _gatling_bullet(self, w: WorldState, team: str, wp: Unit, target: Dict[str, int]) -> None:
        """每颗子弹命中弹道上最近的机器人，10 伤害。"""
        for cell in self._line_cells(wp.pos, target):
            hit = next((r for r in w.alive_robots()
                        if r.pos["x"] == cell["x"] and r.pos["y"] == cell["y"]), None)
            if hit:
                w.pending_damage[hit.id] = w.pending_damage.get(hit.id, 0) + 10
                w.emit("bullet_hit", team=team, robot=hit.id, dmg=10)
                return

    def _railgun_beam(self, w: WorldState, team: str, wp: Unit, target: Dict[str, int]) -> None:
        energy = 10 * wp.level
        robots = w.alive_robots()
        for cell in self._line_cells(wp.pos, target):
            if energy <= 0:
                break
            hit = next((r for r in robots
                        if r.pos["x"] == cell["x"] and r.pos["y"] == cell["y"] and r.alive), None)
            if hit:
                dmg = min(energy, hit.hp)
                w.pending_damage[hit.id] = w.pending_damage.get(hit.id, 0) + dmg
                energy -= dmg
                w.emit("railgun_hit", team=team, robot=hit.id, dmg=dmg)

    def _rocket_hit(self, w: WorldState, team: str, wp: Unit, target: Dict[str, int]) -> None:
        w.pending_damage.setdefault("_rockets", [])
        rockets = w.pending_damage.get("_rockets") or []
        rockets.append({"pos": target, "team": team})
        w.pending_damage["_rockets"] = rockets

    # ------------------------------------------------------------------
    def _resolve_character_moves(self, w: WorldState, intents: Dict[int, Dict[str, int]]) -> None:
        if not intents:
            return
        occ_before = w.occupied(exclude=set(intents))
        robots = {(r.pos["x"], r.pos["y"]): r.id for r in w.alive_robots()}
        results: Dict[int, bool] = {}
        dest_counter: Dict[Tuple[int, int], List[int]] = {}
        for uid, dst in intents.items():
            dest_counter.setdefault((dst["x"], dst["y"]), []).append(uid)
        for uid, dst in intents.items():
            u = w.units[uid]
            key = (dst["x"], dst["y"])
            blocked_by = occ_before.get(key) or robots.get(key)
            if blocked_by:
                results[uid] = False  # ① 目标点受阻
            elif len(dest_counter[key]) > 1:
                results[uid] = False  # ② 目标点争夺
            else:
                # ③ 位置互换
                src = (u.pos["x"], u.pos["y"])
                swapper = next((i for i, d in intents.items()
                                if i != uid and (d["x"], d["y"]) == src), None)
                results[uid] = swapper is None
        for uid, dst in intents.items():
            u = w.units[uid]
            if results[uid]:
                u.pos = dict(dst)
                w.moved_this_round.add(uid)
            team = u.team
            w.last_results[team][uid] = results[uid] and w.last_results.get(team, {}).get(uid, True)
            if not results[uid]:
                w.emit("move_blocked", team=team, unit=uid)

    # ------------------------------------------------------------------
    def _robots_act(self, w: WorldState) -> None:
        cfg = self.cfg.robots
        intents: Dict[int, Tuple[int, int]] = {}
        for r in w.alive_robots():
            if r.abnormal == "dizzy":
                continue
            target = self._robot_pick_target(w, r)
            if not target:
                continue
            step = self._robot_step(w, r, (target.pos["x"], target.pos["y"]), cfg["pathing_model"])
            if step:
                intents[r.id] = step
        dest_counter: Dict[Tuple[int, int], List[int]] = {}
        for rid, dst in intents.items():
            dest_counter.setdefault(dst, []).append(rid)
        occ = w.occupied(exclude={r.id for r in w.alive_robots()})
        robot_cells = {(r.pos["x"], r.pos["y"]): r.id for r in w.alive_robots()}
        for rid, dst in intents.items():
            r = w.units[rid]
            blocker_id = occ.get(dst) or robot_cells.get(dst)
            if blocker_id and blocker_id != rid:
                # 撞上阻挡者 -> 攻击（双方中断移动）
                blocker = w.units.get(blocker_id)
                if blocker and blocker.team != "robot" and (
                        cfg.get("attack_any_blocker", True) or blocker.team == r.target_team):
                    dmg = r.atk
                    w.pending_damage[blocker_id] = w.pending_damage.get(blocker_id, 0) + dmg
                w.emit("robot_attack", robot=rid, target=blocker_id, dmg=r.atk)
            elif len(dest_counter[dst]) > 1:
                w.emit("robot_contest", robot=rid)
            elif dst in robot_cells and robot_cells[dst] != rid:
                w.emit("robot_contest", robot=rid)
            else:
                r.pos = {"x": dst[0], "y": dst[1]}

    def _robot_pick_target(self, w: WorldState, r: Unit) -> Optional[Unit]:
        cands = [u for u in w.units.values()
                 if u.alive and u.team == r.target_team]
        if not cands:
            return None
        return min(cands, key=lambda u: (R.chebyshev(r.pos, u.pos), u.id))

    def _robot_step(self, w: WorldState, r: Unit, goal: Tuple[int, int], model: str) -> Optional[Tuple[int, int]]:
        from agents.framework.pathing import bfs_next_step, greedy_step
        blocked = {(m.pos["x"], m.pos["y"]) for m in w.mines}
        blocked |= {(z["pos"]["x"], z["pos"]["y"]) for z in w.zones}
        blocked |= {(u.pos["x"], u.pos["y"])
                    for u in w.units.values() if u.alive and u.team != "robot"
                    and u.role_type != "station"}  # 建筑不绕行：撞上去攻击
        blocked = {(x, y) for (x, y) in blocked
                   if not any(u.alive and u.team == r.target_team and (u.pos["x"], u.pos["y"]) == (x, y)
                              for u in w.units.values())}
        if model == "bfs":
            return bfs_next_step((r.pos["x"], r.pos["y"]), goal, blocked, w.width, w.height)
        # direct：贪心一步；若目标格被建筑/角色占则攻击（由 _robots_act 处理）
        return greedy_step((r.pos["x"], r.pos["y"]), goal, set(), w.width, w.height)

    # ------------------------------------------------------------------
    def _settle_damage(self, w: WorldState) -> None:
        # 火箭溅射
        rockets = w.pending_damage.pop("_rockets", [])
        for rk in rockets:
            c = rk["pos"]
            for r in w.alive_robots():
                d = R.chebyshev(r.pos, c)
                if d == 0:
                    w.pending_damage[r.id] = w.pending_damage.get(r.id, 0) + 20
                elif d == 1:
                    w.pending_damage[r.id] = w.pending_damage.get(r.id, 0) + 10
        for uid, dmg in list(w.pending_damage.items()):
            u = w.units.get(uid)
            if not u or not u.alive:
                continue
            u.hp -= dmg
            w.emit("damage", unit=uid, dmg=dmg, hp=u.hp)
            if u.hp <= 0:
                self._kill(w, u)
        w.pending_damage = {}

    def _kill(self, w: WorldState, u: Unit) -> None:
        u.alive = False
        w.emit("death", unit=u.id, team=u.team, type=u.role_type)
        if u.team == "robot":
            # 击杀归属：机器人攻击谁的基地，就被谁击杀
            score = R.ROBOT_STATS[u.role_type]["score"]
            w.teams[u.target_team].score_kill += score
        elif u.role_type in ("pioneer", "worker"):
            day = R.day_of(w.round_no)
            next_day_start = (day - 1) * R.ROUNDS_PER_DAY + R.DAY_ROUNDS + 1
            u.revive_at = next_day_start + 19
            u.hp = R.ROLE_HP[u.role_type]
        elif u.role_type == "station":
            w.base_destroyed_round[u.team] = w.round_no
            w.emit("base_destroyed", team=u.team)

    # ------------------------------------------------------------------
    def _end_of_round(self, w: WorldState, day: int) -> None:
        # 复活
        for u in w.units.values():
            if u.team in ("challenger", "defender") and not u.alive and u.revive_at:
                if w.round_no >= u.revive_at and u.role_type in ("pioneer", "worker"):
                    base = next((b for b in w.base_units(u.team)), None)
                    if base:
                        u.alive = True
                        u.revive_at = 0
                        u.pos = {"x": base.pos["x"] - 1, "y": base.pos["y"]}
                        w.emit("revive", unit=u.id)
        # 武器冷却（发射当回合不递减）
        fired = getattr(w, "_fired_weapons", set())
        for u in w.units.values():
            if u.alive and u.cooldown > 0 and u.id not in fired:
                u.cooldown -= 1
        w._fired_weapons = set()
        # 机器人眩晕倒计时
        for r in w.alive_robots():
            if r.abnormal == "dizzy":
                r._dizzy_left = getattr(r, "_dizzy_left", 5) - 1
                if r._dizzy_left <= 0:
                    r.abnormal = ""
        # 任务超时/冷却（刚完成的任务点当回合不递减）
        finished = getattr(w, "_task_finished_this_round", set())
        for team in ("challenger", "defender"):
            for tp in w.task_points[team]:
                if tp.cold_down > 0 and (team, tp.point_id) not in finished:
                    tp.cold_down -= 1
                if tp.active_task and w.round_no - tp.active_task["start_round"] > tp.active_task["timeout_rounds"]:
                    self._finish_task(w, team, tp, completed=False)
        w._task_finished_this_round = set()
        # 矿区枯竭刷新（下回合生效 -> 此处登记，下回合开始时刷新）
        for m in w.mines:
            if m.remaining <= 0 and m.refresh_at is None:
                m.refresh_at = w.round_no + 1
        for m in [m for m in w.mines if getattr(m, "refresh_at", None) == w.round_no]:
            w.mines.remove(m)
            m.pos = self._rand_free(w)
            m.remaining = self.cfg.economy["mine_capacity"]
            m.refresh_at = None
            w.mines.append(m)
            w.emit("mine_refresh", kind=m.kind)
        # 生存积分（每日最后回合）
        if R.is_day_last_round(w.round_no):
            for t, ts in w.teams.items():
                if any(b.alive for b in w.base_units(t)):
                    ts.score_survive += 10 * day
        # 胜负预判（摧毁回合在 _kill 记录，最终判定见 score.py）
        alive = {t: any(b.alive for b in w.base_units(t)) for t in ("challenger", "defender")}
        if not alive["challenger"] and not alive["defender"]:
            w.winner = "both_destroyed"
        # 召唤令清空（已在本夜波次使用）
        for t, ts in w.teams.items():
            if R.is_night_first_round(w.round_no) and ts.summon_orders_pending:
                ts.summon_orders_pending = []

    def _rand_free(self, w: WorldState) -> Dict[str, int]:
        for _ in range(500):
            x = self.rng.randrange(w.width)
            y = self.rng.randrange(w.height)
            p = {"x": x, "y": y}
            if (x, y) not in w.occupied() and w.zone_distance(p, "challenger") > 6 \
                    and w.zone_distance(p, "defender") > 6:
                return p
        return {"x": self.rng.randrange(w.width), "y": self.rng.randrange(w.height)}

    # ------------------------------------------------------------------
    def _spawn_robots(self, w: WorldState, day: int) -> None:
        cfg = self.cfg.robots
        for team in ("challenger", "defender"):
            ts = w.teams[team]
            base = next((b for b in w.base_units(team)), None)
            if not base:
                continue
            for rt, stats in R.ROBOT_STATS.items():
                n = self.cfg.wave_count(rt, day)
                for _ in range(n):
                    self._spawn_robot(w, team, rt, base.pos, cfg)
            # 召唤令叠加
            for rt in ts.summon_orders_pending:
                self._spawn_robot(w, team, rt, base.pos, cfg, summoned=True)
                w.emit("summoned_robot", team=team, robot=rt)

    def _spawn_robot(self, w: WorldState, team: str, rt: str, near: Dict[str, int], cfg: dict, summoned=False) -> None:
        rad = cfg["spawn_radius"]
        stats = R.ROBOT_STATS[rt]
        for _ in range(50):
            x = near["x"] + self.rng.randint(-rad, rad)
            y = near["y"] + self.rng.randint(-rad, rad)
            if 0 <= x < w.width and 0 <= y < w.height and (x, y) not in w.occupied():
                rid = w.next_robot_id()
                w.units[rid] = Unit(rid, "robot", {"x": x, "y": y}, rt, hp=stats["hp"],
                                    atk=stats["atk"], target_team=team)
                if summoned:
                    w.units[rid]._summoned = True
                return

    # ------------------------------------------------------------------
    def _zone_pos(self, w: WorldState, neutral: str) -> Dict[str, int]:
        return next(z["pos"] for z in w.zones if z["neutralType"] == neutral)

    def _task_point_at(self, w: WorldState, team: str, pos: Dict[str, int]) -> Optional[TaskPointState]:
        for tp in w.task_points[team]:
            if any(R.adjacent(pos, p) for p in tp.positions):
                return tp
        return None
