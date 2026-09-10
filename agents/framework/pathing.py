# -*- coding: utf-8 -*-
"""agent 侧寻路与地图工具：基于视野内静态障碍的 BFS / 贪心步进。

agent 只能看见自己视野内的障碍，障碍集合由调用方给定时，
路径只是"计划"——真实碰撞由引擎结算。
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Set, Tuple

DIRS8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]


def neighbors(pos: Tuple[int, int], width: int, height: int) -> List[Tuple[int, int]]:
    out = []
    for dx, dy in DIRS8:
        nx, ny = pos[0] + dx, pos[1] + dy
        if 0 <= nx < width and 0 <= ny < height:
            out.append((nx, ny))
    return out


def bfs_next_step(start: Tuple[int, int], goal: Tuple[int, int],
                  blocked: Set[Tuple[int, int]], width: int, height: int) -> Optional[Tuple[int, int]]:
    """返回从 start 朝 goal 的下一格；start==goal 或无路时返回 None。"""
    if start == goal:
        return None
    prev: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {start: None}
    q = deque([start])
    while q:
        cur = q.popleft()
        if cur == goal:
            break
        for nxt in neighbors(cur, width, height):
            if nxt not in prev and nxt not in blocked:
                prev[nxt] = cur
                q.append(nxt)
    if goal not in prev:
        return None
    # 回溯到 start 的下一格
    node = goal
    while prev[node] is not None and prev[node] != start:
        node = prev[node]
    return node if prev[node] == start else None


def greedy_step(start: Tuple[int, int], goal: Tuple[int, int],
                blocked: Set[Tuple[int, int]], width: int, height: int) -> Optional[Tuple[int, int]]:
    """切比雪夫贪心：优先缩小距离的可通行格。"""
    if start == goal:
        return None
    cands = [n for n in neighbors(start, width, height) if n not in blocked]
    if not cands:
        return None
    cands.sort(key=lambda c: (max(abs(c[0] - goal[0]), abs(c[1] - goal[1])), c))
    best = cands[0]
    if max(abs(best[0] - goal[0]), abs(best[1] - goal[1])) >= max(abs(start[0] - goal[0]), abs(start[1] - goal[1])):
        return None
    return best


def adjacent_free_cell(pos: Tuple[int, int], goal: Tuple[int, int],
                       blocked: Set[Tuple[int, int]], width: int, height: int) -> Optional[Tuple[int, int]]:
    """找 pos 周围一格中距 goal 最近的空格（用于贴靠目标）。"""
    cands = [n for n in neighbors(pos, width, height)
             if n not in blocked and max(abs(n[0] - goal[0]), abs(n[1] - goal[1])) <= 1]
    if cands:
        return cands[0]
    cands = [n for n in neighbors(pos, width, height) if n not in blocked]
    if not cands:
        return None
    cands.sort(key=lambda c: max(abs(c[0] - goal[0]), abs(c[1] - goal[1])))
    return cands[0]
