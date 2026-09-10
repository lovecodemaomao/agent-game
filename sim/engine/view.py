# -*- coding: utf-8 -*-
"""request 视图构建（任务4.2）：由世界状态生成《接口文档》格式的 request。

以 docs/request.txt 为 golden 快照做字段级验收。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from common import rules as R
from sim.engine.config import EngineConfig
from sim.engine.state import WorldState

ENEMY_EXPOSED_FIELDS = ("id", "pos", "roleType", "health", "attackPower", "attackRange", "level")


def build_request(w: WorldState, cfg: EngineConfig, team: str,
                  llm_resp: str = "", errors: Optional[List[Dict]] = None,
                  task_desc_provider=None) -> Dict[str, Any]:
    enemy = "defender" if team == "challenger" else "challenger"
    ts = w.teams[team]
    round_no = w.round_no

    # -- zones --
    zones = [dict(z) for z in w.zones]
    for m in w.mines:
        zones.append({"neutralType": m.kind, "pos": dict(m.pos)})

    # -- teamOur --
    our_roles = [u.to_role_json() for u in w.units.values()
                 if u.team == team and u.alive]
    our_roles.sort(key=lambda r: r["id"])
    player_tasks = []
    for tp in w.task_points[team]:
        player_tasks.append({
            "taskType": f"自进化类{tp.point_id.replace('point', '')}",
            "taskPosition": dict(tp.positions[0]),
            "coldDownRounds": tp.cold_down,
            "scoreReward": tp.score_reward,
            "goldReward": tp.gold_reward,
            "isValid": tp.remaining_total > 0 and tp.cold_down == 0 and tp.active_task is None,
            "timeoutRounds": tp.timeout_rounds,
        })
    team_our = {
        "type": team,
        "teamId": "6324" if team == "challenger" else "6325",
        "teamName": "Challenger" if team == "challenger" else "Defender",
        "goldNum": ts.gold,
        "totalScore": ts.total_score,
        "playerTasks": player_tasks,
        "roles": our_roles,
    }

    # -- teamEnemy（视野过滤）--
    vis = w.visible_enemy_ids(team)
    enemy_roles = []
    for u in w.units.values():
        if u.team == enemy and u.alive and u.id in vis:
            j = {f: v for f, v in u.to_role_json().items() if f in ENEMY_EXPOSED_FIELDS}
            enemy_roles.append(j)
    enemy_roles.sort(key=lambda r: r["id"])

    # -- robot（全图可见）--
    robots = []
    for u in w.alive_robots():
        robots.append({
            "id": u.id, "pos": dict(u.pos), "roleType": u.role_type,
            "health": u.hp, "abnormalState": u.abnormal,
            "targetTeam": u.target_team,
        })
    robots.sort(key=lambda r: r["id"])

    # -- 结果回填 --
    last_results = {str(k): bool(v) for k, v in w.last_results.get(team, {}).items()}
    vendor_list = [{"name": o, "price": int(w.news_prices.get(o, cfg.economy["base_prices"][o]))}
                   for o in R.ORES]
    shop_list = [{"name": n, "price": p} for n, p in R.WEAPON_SHOP_ITEMS.items()]

    req = {
        "roundNo": round_no,
        "mapInfo": {"width": w.width, "height": w.height, "zones": zones},
        "teamOur": team_our,
        "teamEnemy": {"roles": enemy_roles},
        "robot": {"roles": robots},
        "phaseTask": w.phase_task.get(team, ""),
        "lastRoundRoleActionResults": last_results,
        "lastSummonTreasureResult": w.last_summon_result.get(team, 0),
        "llmResp": llm_resp,
        "worldNews": {
            "officialNews": w.world_news.get("officialNews", ""),
            "folkLegends": w.world_news.get("folkLegends", ""),
        },
        "lastCmdResult": w.last_cmd_result.get(team, ""),
        "vendorShopList": vendor_list,
        "weaponShopList": shop_list,
        "errors": errors or [],
    }
    return req


def diff_against_demo() -> List[str]:
    """与 docs/request.txt 做字段级对比（结构对齐，忽略数值差异）。"""
    from tests.util import load_demo_request
    demo = load_demo_request()

    def shape(o, depth=0):
        if isinstance(o, dict):
            return {k: shape(v, depth + 1) for k, v in sorted(o.items())}
        if isinstance(o, list):
            return [shape(o[0], depth + 1)] if o else []
        return type(o).__name__

    from sim.engine.config import load_config
    from sim.engine.state import WorldState
    cfg = load_config()
    w = WorldState.create(cfg)
    w.round_no = 85
    built = build_request(w, cfg, "challenger")
    missing = sorted(set(shape(demo)) - set(shape(built)))
    extra = sorted(set(shape(built)) - set(shape(demo)))
    return [f"missing: {missing}" if missing else "", f"extra: {extra}" if extra else ""]
