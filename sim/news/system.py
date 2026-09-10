# -*- coding: utf-8 -*-
"""中立新闻系统（任务组8）。

设计（design D4）：参数先行、LLM 措辞。
- 事件参数（矿种/影响天数/价格倍率/停采）与宝藏谜题答案在开局确定性采样并锁定
- LLM 仅把参数写成叙事文本（独立上下文，见 proxy.news_chat）；不可用时回退模板
- 世界影响由引擎按参数结算，文本只需"可推断"参数
"""
from __future__ import annotations

import random
from typing import Callable, Dict, List, Optional, Tuple

from common import rules as R

ORE_CN = {"stone": "石矿", "iron": "铁矿", "copper": "铜矿"}
DIR_CN = ["东", "南", "西", "北"]

OFFICIAL_TEMPLATES = {
    "collapse": (
        "【官方消息】矿业管理局紧急通报：{region}{ore}区昨夜发生严重矿井塌方事故，主巷道结构受损。"
        "安全监察部门已下达停工通知：自明日 起全面停工约{duration}天进行巷道加固。"
        "贸易行会预计：受供应短缺影响，{ore}收购价将上涨至{mult}倍，恢复开采后价格回落。"),
    "discovery": (
        "【官方消息】地质勘探队今日宣布：在{region}发现大型{ore}富矿脉，开采准入即刻放开，日产量将大增。"
        "贸易行会预计：未来{duration}天内{ore}供应过剩，收购价跌至{mult}倍。"),
    "surge": (
        "【官方消息】王国军械司下达紧急订单，大量收购{ore}用于军备生产，为期{duration}天。"
        "小贩联合会随即上调{ore}收购价至{mult}倍，逾期恢复。"),
    "calm": "【官方消息】今日无重大新闻。",
}


class NewsSystem:
    """按天生成官方消息与民间传闻；持有并锁定宝藏谜题答案。"""

    def __init__(self, cfg, seed: int, width: int = 41, height: int = 32,
                 llm_chat: Optional[Callable[[str], str]] = None):
        self.cfg = cfg
        self.rng = random.Random(seed + 999)
        self.llm_chat = llm_chat
        self.width, self.height = width, height
        self.treasure = self._gen_treasure()
        self._event: Dict = {}
        self._clue_day = 0

    # ------------------------------------------------------------------
    def _gen_treasure(self) -> Dict:
        tc = self.cfg.treasure
        # 地点：远离双方基地的随机点（以相对基准点编码进线索）
        bx, by = 10, 24  # 挑战者基地（线索锚点）
        for _ in range(200):
            x = self.rng.randrange(4, self.width - 4)
            y = self.rng.randrange(4, self.height - 4)
            if abs(x - bx) + abs(y - by) >= 12:
                break
        items = self.rng.sample(list(R.TASK_ITEMS), tc["items_needed"])
        window = (tc["window_start"], tc["window_end"])
        return {"pos": {"x": x, "y": y}, "items_needed": items,
                "window": window, "reward_score": tc["reward_score"],
                "reward_gold": tc["reward_gold"]}

    # ------------------------------------------------------------------
    def new_day(self, day: int, world) -> None:
        """在每天第一个回合前调用：生成新闻并施加世界影响。"""
        self._official_event(day, world)
        self._folk_legend(day, world)

    # -- 官方消息：参数先行 --
    def _official_event(self, day: int, world) -> None:
        ncfg = self.cfg.news
        if day == 1 or self.rng.random() > ncfg["official_event_prob"]:
            self._apply_text(world, OFFICIAL_TEMPLATES["calm"])
            return
        ore = self.rng.choice(list(R.ORES))
        kind = self.rng.choice(["collapse", "discovery", "surge"])
        region = self.rng.choice(["北部", "南部", "东部", "西部"])
        if kind == "collapse":
            dur, mult = ncfg["collapse_duration"], ncfg["collapse_price_mult"]
        elif kind == "discovery":
            dur, mult = ncfg["discovery_duration"], ncfg["discovery_price_mult"]
        else:
            dur, mult = ncfg["surge_duration"], ncfg["surge_price_mult"]
        self._event = {"kind": kind, "ore": ore, "start_day": day + 1,
                       "duration": dur, "price_mult": mult}
        text = OFFICIAL_TEMPLATES[kind].format(
            region=region, ore=ORE_CN[ore], duration=dur, mult=mult)
        self._apply_text(world, self._maybe_llm(text))

    def _apply_event_effects(self, day: int, world) -> None:
        ev = self._event
        base = self.cfg.economy["base_prices"]
        if not ev:
            return
        in_effect = ev["start_day"] <= day < ev["start_day"] + ev["duration"]
        for o in R.ORES:
            world.news_prices[o] = base[o]
            world.news_mine_blocked[o] = 0
        if not in_effect:
            if day >= ev["start_day"] + ev["duration"]:
                self._event = {}  # 影响结束
            return
        ore = ev["ore"]
        world.news_prices[ore] = base[ore] * ev["price_mult"]
        if ev["kind"] == "collapse":
            world.news_mine_blocked[ore] = ev["start_day"] + ev["duration"] - day
        elif ev["kind"] == "discovery":
            world.news_mine_blocked[ore] = 0

    def _apply_text(self, world, text: str) -> None:
        world.world_news["officialNews"] = text

    def _maybe_llm(self, fallback_text: str) -> str:
        if not self.llm_chat:
            return fallback_text
        try:
            prompt = ("请把下面的新闻改写为更生动的版本（120字内）。"
                      "硬性要求：矿种、天数、价格倍数等事实必须原样保留，不得增删数字。\n" + fallback_text)
            out = self.llm_chat(prompt).strip()
            return out or fallback_text
        except Exception:
            return fallback_text

    # -- 民间传闻：宝藏线索链 --
    def _folk_legend(self, day: int, world) -> None:
        tcfg = self.cfg.treasure
        t = self.treasure
        clues: List[str] = []
        if day == 1:
            dx, dy = t["pos"]["x"] - 10, t["pos"]["y"] - 24
            clues.append(f"村东老叟临终留言：宝藏埋在挑战者营地{'东' if dx >= 0 else '西'}{abs(dx)}里、"
                         f"{'北' if dy >= 0 else '南'}{abs(dy)}里之处")
        elif day == 2:
            it0 = t["items_needed"][0]
            clues.append(f"游方道士曰：开启宝藏需以「{ITEM_CN.get(it0, it0)}」为引")
        elif day == 3:
            rest = t["items_needed"][1:]
            names = "、".join(ITEM_CN.get(i, i) for i in rest)
            clues.append(f"酒馆诗人吟唱：祭坛之前还需备齐{names}")
        elif day == 4:
            clues.append(f"星象师推演：祭坛自第{t['window'][0]}天起方可开启")
        elif day == 5:
            clues.append(f"古老石碑刻着：宝藏封印至第{t['window'][1]}天闭合，逾期永闭")
        else:
            clues.append("村中再无新的传闻。")
        item_cn = "、".join(ITEM_CN.get(i, i) for i in t["items_needed"])
        text = (f"【民间传闻】{clues[-1]}（提示：宝藏唯一，需开拓者携齐 {item_cn} "
                f"在开启窗口内前往祭坛召唤）") if day <= tcfg["clue_days"] else f"【民间传闻】{clues[-1]}"
        world.world_news["folkLegends"] = self._maybe_llm(text)


ITEM_CN = {
    "AcientTablet": "古符石板", "StarSand": "星辰之沙", "FlameBreath": "烈焰之息",
    "FrostPotion": "寒霜药剂", "ThornAmulet": "荆棘护符", "IronWhistle": "回音铁哨",
}
