# -*- coding: utf-8 -*-
"""出站指令严格校验器（agent 生命线）。

任何 response 提交到模拟平台/判题器之前必须通过本模块校验。
纯确定性规则、零 LLM：
  1. schema 层：动作码合法、必填字段齐备、类型正确（对应赛制"指令错误"，计入异常）
  2. 语义层：坐标域、ID 归属、昼夜门、角色类型门、多目标数量、加特林锥形
  3. 兜底层：非法指令剔除（不出站），绝不让非法报文离开进程

双方 agent（competitor / baseline）共用同一实现。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

from common import rules as R


class ValidationContext:
    """由当前 request 派生的静态校验上下文。"""

    def __init__(self, request: Dict[str, Any]):
        our = request.get("teamOur", {})
        self.round_no: int = int(request.get("roundNo", 1))
        self.day: int = R.day_of(self.round_no)
        self.phase: str = R.phase_of(self.round_no)
        self.width: int = int(request.get("mapInfo", {}).get("width", 41))
        self.height: int = int(request.get("mapInfo", {}).get("height", 32))
        roles = our.get("roles", []) or []
        self.own_ids: Set[int] = {int(r["id"]) for r in roles}
        self.role_type: Dict[int, str] = {int(r["id"]): r["roleType"] for r in roles}
        self.character_ids: Set[int] = {i for i, t in self.role_type.items() if t in ("pioneer", "worker")}
        self.weapon_level: Dict[int, int] = {
            int(r["id"]): int(r.get("level", 1))
            for r in roles if r["roleType"] in R.WEAPON_TYPES
        }
        self.weapon_pos: Dict[int, Dict[str, int]] = {
            int(r["id"]): {"x": int(r["pos"]["x"]), "y": int(r["pos"]["y"])}
            for r in roles if r["roleType"] in R.WEAPON_TYPES
        }
        self.weapon_kind: Dict[int, str] = {
            int(r["id"]): r["roleType"] for r in roles if r["roleType"] in R.WEAPON_TYPES
        }
        self.weapon_ids: Set[int] = set(self.weapon_level)


def _to_int(v: Any) -> Optional[int]:
    """宽松取整：int/整值 float/数字字符串 -> int，否则 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
                return int(f) if f.is_integer() else None
            except ValueError:
                return None
    return None


def _coerce_pos(p: Any) -> Optional[Dict[str, int]]:
    """接受 {"x":..,"y":..} / [x,y] / "x,y"，严格化为 {"x":int,"y":int}。"""
    if isinstance(p, dict):
        x, y = _to_int(p.get("x")), _to_int(p.get("y"))
    elif isinstance(p, (list, tuple)) and len(p) == 2:
        x, y = _to_int(p[0]), _to_int(p[1])
    elif isinstance(p, str) and "," in p:
        a, b = p.split(",", 1)
        x, y = _to_int(a), _to_int(b)
    else:
        return None
    if x is None or y is None:
        return None
    return {"x": x, "y": y}


def _in_map(pos: Dict[str, int], ctx: ValidationContext) -> bool:
    return 0 <= pos["x"] < ctx.width and 0 <= pos["y"] < ctx.height


def _gatling_cone_ok(weapon_pos: Dict[str, int], targets: List[Dict[str, int]]) -> bool:
    """所有目标相对武器的方向两两夹角 ≤ 90°（向量点积 ≥ 0）。"""
    vecs = []
    for t in targets:
        dx, dy = t["x"] - weapon_pos["x"], t["y"] - weapon_pos["y"]
        if dx == 0 and dy == 0:
            return False
        vecs.append((dx, dy))
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            if vecs[i][0] * vecs[j][0] + vecs[i][1] * vecs[j][1] < 0:
                return False
    return True


def validate_command(key: str, cmd: Any, ctx: ValidationContext) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验单条指令。返回 (规范化指令, 拒绝原因)；合法时原因与拒绝原因为 None。"""
    kid = _to_int(key)
    if kid is None or kid not in ctx.own_ids:
        return None, f"key {key!r} 不是本队角色ID"
    if not isinstance(cmd, dict):
        return None, "指令不是对象"
    action = cmd.get("action")
    if action not in R.ACTIONS:
        return None, f"动作码非法: {action!r}"

    spec = R.COMMAND_FIELDS[action]
    out: Dict[str, Any] = {"action": action}

    # -- 必填字段 --
    for f in spec["required"]:
        if f not in cmd or cmd[f] in (None, ""):
            return None, f"{action} 缺少必填字段 {f}"

    # -- targetPos --
    if "targetPos" in cmd or action in R.POS_ACTIONS:
        raw = cmd.get("targetPos")
        if raw is None:
            return None, f"{action} 缺少 targetPos"
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        poss = []
        for p in raw:
            pos = _coerce_pos(p)
            if pos is None:
                return None, f"{action} targetPos 元素非法: {p!r}"
            if not _in_map(pos, ctx):
                return None, f"{action} targetPos 越界: {pos}"
            poss.append(pos)
        if not poss:
            return None, f"{action} targetPos 为空"
        out["targetPos"] = poss

    # -- controllerId --
    if action == "attack":
        cid = _to_int(cmd.get("controllerId"))
        if cid is None:
            return None, "attack controllerId 非法"
        if cid not in ctx.character_ids:
            return None, f"attack controllerId {cid} 不是本队角色"
        if kid not in ctx.weapon_ids:
            return None, f"attack 挂载键 {kid} 不是本队武器工事"
        lvl = ctx.weapon_level.get(kid, 1)
        need = 1 if ctx.role_type[kid] == "railgun" else lvl
        if len(out["targetPos"]) != need:
            return None, f"attack 目标数量 {len(out['targetPos'])} 与武器等级不符（需 {need}）"
        if ctx.weapon_kind.get(kid) == "gatling" and not _gatling_cone_ok(
                ctx.weapon_pos[kid], out["targetPos"]):
            return None, "加特林多目标未落在同一 90° 锥形内"
        out["controllerId"] = str(cid)

    # -- name / num --
    if "name" in spec["required"] + spec.get("optional", []):
        name = cmd.get("name")
        if name is not None:
            if not isinstance(name, str) or not name:
                return None, "name 必须为非空字符串"
            out["name"] = name
    if action == "build" and out.get("name") not in R.BUILDABLE:
        return None, f"build.name 非法: {out.get('name')!r}"
    if action == "sell" and out.get("name") not in R.ORES:
        return None, f"sell.name 非法（仅支持 stone/iron/copper）: {out.get('name')!r}"
    if action == "use":
        n = out.get("name", "")
        if n not in R.WEAPON_SHOP_ITEMS:
            return None, f"use.name 未知物品: {n!r}"
        if n in R.USE_NEEDS_POS and "targetPos" not in out:
            return None, f"use {n} 必须指定 targetPos"
    if "num" in cmd and cmd.get("num") is not None:
        num = _to_int(cmd.get("num"))
        if num is None or num < 1:
            return None, f"num 非法: {cmd.get('num')!r}"
        out["num"] = num

    # -- taskAnswer / item --
    if action == "submitAnswer":
        ta = cmd.get("taskAnswer")
        if not isinstance(ta, str):
            ta = str(ta)
        out["taskAnswer"] = ta
    if action == "summonTreasure":
        items = cmd.get("item")
        if not isinstance(items, (list, tuple)) or not items:
            return None, "summonTreasure.item 必须为非空数组"
        norm = []
        for it in items:
            if not isinstance(it, str) or not it:
                return None, f"item 元素非法: {it!r}"
            norm.append(it)
        out["item"] = norm

    # -- 昼夜门 / 角色类型门 --
    rtype = ctx.role_type[kid]
    if action == "attack" and ctx.phase != "night":
        return None, "attack 仅夜晚可用"
    if action == "build" and ctx.phase != "day":
        return None, "build 仅白天可用"
    if action in ("build", "remove", "collect") and rtype != "worker":
        return None, f"{action} 仅工人可用"
    if action in ("acceptTask", "submitAnswer", "summonTreasure") and rtype != "pioneer":
        return None, f"{action} 仅开拓者可用"

    return out, None


def validate_response(resp: Any, ctx: ValidationContext) -> Tuple[Dict[str, Any], List[str]]:
    """校验并清洗整个 response。

    返回 (干净response, 拒绝原因列表)。保证输出结构永远合法：
    {"roleCommandMap": {...}, "prompt": str, "executeCmd": str}
    """
    dropped: List[str] = []
    clean: Dict[str, Any] = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
    if not isinstance(resp, dict):
        return clean, ["response 不是对象"]

    raw_map = resp.get("roleCommandMap")
    if isinstance(raw_map, dict):
        # 操控武器的角色本回合不能另有动作（一角色一回合一指令）
        controllers: Set[int] = set()
        for k, c in raw_map.items():
            if isinstance(c, dict) and c.get("action") == "attack":
                cid = _to_int(c.get("controllerId"))
                if cid is not None:
                    controllers.add(cid)
        seen_controller: Dict[int, str] = {}
        for k, c in raw_map.items():
            fixed, reason = validate_command(k, c, ctx)
            kid = _to_int(k)
            if fixed is not None and kid in controllers and fixed["action"] != "attack":
                dropped.append(f"角色 {kid} 正在操控武器，剔除其独立指令")
                continue
            if fixed is not None and kid in controllers and kid in seen_controller:
                dropped.append(f"角色 {kid} 操控多座武器，仅保留第一座")
                continue
            if fixed is not None:
                clean["roleCommandMap"][str(kid)] = fixed
                seen_controller[kid] = fixed["action"]
            elif reason:
                dropped.append(f"[{k}] {reason}")

    for f in ("prompt", "executeCmd"):
        v = resp.get(f, "")
        if v is None:
            v = ""
        if not isinstance(v, str):
            v = str(v)
        clean[f] = v

    return clean, dropped
