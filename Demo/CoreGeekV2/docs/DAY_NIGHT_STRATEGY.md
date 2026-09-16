可以，我给你整理成一版**可以直接丢给 Codex 更新策略逻辑**的说明，重点只保留现在已经讨论清楚的白天/夜晚决策，不再额外扩架构。

# 昼夜策略更新说明

本次只调整策略逻辑，目标是：

* 白天：围绕 70 回合做全局资源规划，尽可能提高采矿和经济效率，同时保证夜晚前完成防御建设并全部就位。
* 夜晚：策略尽量固定，不做复杂全局规划，以稳定防守为主。
* 不再使用大量“第几回合固定做什么”的硬编码。
* 不引入强化学习、复杂搜索或新的重型架构。
* 优先保持逻辑简单、稳定、可解释。

---

# 一、角色总体分工

三个角色白天的职责原则上固定。

## Worker A：主经济工人

主要职责：

```text
采铜 / 铁
→ 最大化经济收益
→ 必要时辅助建墙
→ 白天结束前到夜间岗位
```

主要目标是尽量把安全的白天回合转化成矿物和金币。

---

## Worker B：工程工人

主要职责：

```text
优先满足当天 StoneQuota
→ 石头够后转铜 / 铁
→ 白天末尾执行 build / remove
→ 到夜间岗位
```

只有工人可以：

```text
build
remove
collect
```

因此真正的建筑施工由 Worker B 为主负责。

Day1 建筑量较大时，Worker A 可以协助建造。

---

## Pioneer：任务 + 采购物流

优先级：

```text
Task
>
已有物品交付 / 使用
>
采购
>
武器商店附近待命
>
夜间回位
```

开拓者不能采矿，因此空闲时间不要回基地闲置。

默认行为：

```text
没有可做 Task
→ 去武器商店附近等待
```

这样金币一到账，就可以立即采购。

Pioneer 主要负责：

```text
buy
use
WallFixer
建筑升级券
任务用品
```

尽量不要让工人为了采购绕去商店。

---

# 二、白天总体逻辑

白天不再按照：

```text
前30回合干什么
前40回合干什么
Round 50固定返程
```

进行控制。

改成：

```text
当天开始
↓
生成 DailyPlan
↓
三角色并行行动
↓
动态采矿
↓
达到各自返程截止时间
↓
统一收尾
↓
夜间岗位
```

---

# 三、DailyPlan

每天白天第一个回合，根据当前状态生成当天计划。

只需要确定：

```text
1. 今天必须建造什么
2. 今天需要多少 stone
3. 今天希望升级什么
4. 哪些建筑需要维护
5. 预计需要多少 gold
```

建议数据概念：

```text
DailyPlan

required_builds
required_stone
desired_upgrades
required_repairs
gold_targets
```

注意：

```text
今日金钱目标达到
≠
停止采矿
```

如果安全时间仍然充足：

```text
继续采矿
→ 为后续日期积累资源和金币
```

---

# 四、阶段性战略

## Day1

硬目标：

```text
Rocket ×2
Railgun ×1

正面围墙 ×4
```

“正面”指：

```text
基地朝向敌方基地的一面
```

三座武器优先建齐。

初始金币 75，刚好可以覆盖三座 25 金币武器。

石头只需要优先保证正面墙需求。

例如：

```text
StoneQuota = 4
```

一旦满足：

```text
停止专门采石
→ 两工人转铜 / 铁
```

Day1 剩余经济：

```text
争取升级 1~2 座武器
```

推荐优先：

```text
Rocket
→ Rocket
→ Railgun
```

但具体能升几个取决于当天收入。

---

## Day2

核心：

```text
继续升级武器
+
补半边围墙
```

主要目标：

```text
尽量使三座武器达到 Lv2
```

同时将围墙从：

```text
正面4格
```

逐渐扩展成：

```text
基地面向敌方方向的半圈防御
```

围墙布局最好由预定义 WallLayout 控制。

例如：

```text
Tier A = Day1 正面4格
Tier B = 向两侧扩展
Tier C = 后续完整防线
```

不要在经济代码里写死具体墙坐标逻辑。

---

## Day3

进入防御质量提升阶段：

```text
基地升级
+
武器继续升级
+
关键围墙升级
```

总体目标：

```text
Station Lv2

然后：
Weapon Lv3

然后：
实际承伤的关键墙升级
```

但是基地和围墙升级不要简单“有钱立即使用”。

见后面的“残血升级策略”。

---

## Day4+

不再建立 Day4 / Day5 / Day6 的大量固定计划。

统一进入成熟期：

```text
紧急防御维护
>
武器升满
>
基地强化
>
关键围墙升级 / 修复
>
其他围墙维护
>
富余经济用途
```

整体原则：

```text
先保证生存
→ 再强化火力
→ 再提高防线质量
```

---

# 五、采矿算法

两个工人的核心目标是：

> 在保证最终事务链和夜间回位的前提下，把尽可能多的白天回合转化为矿物。

---

## 矿点基本规则

每个矿总共：

```text
10次 collect
```

每次：

```text
1回合
→ 获得1个矿物
```

矿采光后：

```text
矿消失
→ 下一回合随机刷新新矿
```

因此：

> 优先完整采光矿点。

因为采光不仅获得剩余矿物，还能主动触发一个新矿刷新。

---

## 中间矿原则

正常情况下：

```text
中途选中的矿
→ 尽量采光
```

最后一个矿：

```text
如果临近返程截止时间
→ 允许部分采集
```

即：

```text
中间矿尽量采光
只有最后一个矿允许残留
```

---

# 六、矿点评分

不要仅按照：

```text
铜 > 铁 > 石
```

也不要大量人工权重。

基础评价：

```text
OreValue =
remaining_ore × current_price
```

基础效率：

```text
MiningEfficiency =
OreValue
/
(TravelCost + CollectCost)
```

更推荐使用增量成本：

```text
ExtraCost =
去矿
+ 采矿
+ 从矿点开始的最终收尾成本
- 当前立即收尾成本
```

然后：

```text
Score =
OreValue / ExtraCost
```

这样会自然考虑：

```text
矿距离
小贩距离
基地距离
商店距离
夜间返程距离
```

---

# 七、采光奖励

矿越接近清空，应该越优先。

例如：

```text
剩余 1~3
→ 强 completion bonus

剩余 4~6
→ 小 bonus

完整新矿
→ 无额外 bonus
```

但返程安全永远优先。

---

# 八、双工人协同

两个工人不要固定：

```text
Worker1 永远左边
Worker2 永远右边
```

矿是动态刷新的。

每次需要选新矿时，联合计算：

```text
Worker A → Mine X
Worker B → Mine Y
```

枚举少量候选组合。

评分：

```text
PairScore =
Score(A, X)
+
Score(B, Y)
-
OverlapPenalty
```

同一矿默认禁止：

```text
A 和 B 不要同时选择同一个矿
```

目标是让两个工人自然向不同方向展开。

除非特殊情况下两人原本就在同一个即将清空的矿附近，否则不要为了合作横跨地图。

---

# 九、StoneQuota

Stone 不应作为普通经济矿处理。

每天首先计算：

```text
StoneQuota
=
当天待建墙数量
+
预计需要重建的墙
+
少量安全余量
```

如果：

```text
当前石头 < StoneQuota
```

Worker B 提高石矿优先级。

一旦：

```text
StoneQuota 满足
```

石头恢复普通经济价值。

多余 stone 可以出售。

不要再使用：

```python
SELLABLE = ('iron', 'copper')
```

stone 也允许出售。

---

# 十、动态返程截止

不要使用固定：

```text
Round 45停止采矿
Round 50回家
```

每个角色单独计算 TailCost。

例如 Worker：

```text
当前位置
→ Vendor
→ Build区域
→ 夜间岗位
```

Pioneer：

```text
当前位置
→ Shop
→ 目标建筑
→ use
→ 夜间岗位
```

定义：

```text
TailCost(position)
```

然后：

```text
MiningBudget =
70
- 当前白天回合
- TailCost
- SafetyMargin
```

如果剩余时间不足：

```text
去下一矿
+ 采完整矿
+ 最终事务链
```

就停止开启新的完整矿。

此时：

```text
附近还有矿
→ 允许最后部分采几次

然后立即 FinalRoute
```

---

# 十一、白天最终事务链

尽量只进行一次完整收尾。

### Worker A

```text
最后矿
→ 必要时 Vendor
→ 夜间岗位
```

### Worker B

```text
最后矿
→ 必要时 Vendor
→ 建墙 / remove / 重建
→ 夜间岗位
```

### Pioneer

```text
Task / Shop
→ buy
→ use / repair / upgrade
→ 夜间岗位
```

尽量避免：

```text
上午跑一次商店
下午又跑一次商店
晚上再回来
```

---

# 十二、Pioneer Task逻辑

Task 是 Pioneer 第一优先级。

如果存在可接任务：

```text
先判断今天是否来得及安全完成
```

条件大致：

```text
去任务点
+
预计任务时间
+
任务点到夜间岗位
+
安全余量
<= 剩余白天时间
```

满足：

```text
前往 TaskPoint
→ acceptTask
```

一旦 Task 开始：

```text
普通经济逻辑不得抢占 Pioneer
```

因为开拓者离开任务点附近会导致任务结束。

Task 完成后：

```text
优先去武器商店附近待命
```

---

# 十三、Pioneer采购逻辑

Pioneer 在商店附近待命的主要目的：

```text
工人卖矿
→ 团队金币增加
→ Pioneer立即购买
```

卖矿和买东西不需要是同一个角色。

例如：

```text
Worker A:
sell

下一回合：

Pioneer:
buy WeaponUpgradeVoucher
```

这样工人不用再绕去商店。

---

# 十四、采购和使用必须分离

非常重要：

```text
WantUpgrade
VoucherOwned
UseNow
```

是三个不同状态。

不能：

```text
buy
→ 必定马上 use
```

---

# 十五、武器升级

武器升级：

```text
一般买到后尽快使用
```

原因：

```text
武器不承担主要肉盾作用
提高等级直接提升下一晚战斗能力
```

优先级整体：

```text
Rocket
→ Rocket
→ Railgun
```

但如果具体实战发现 Railgun升级收益更高，可以后续调整优先级表，不修改调度框架。

---

# 十六、基地 / 围墙“残血升级”

基地和围墙升级会：

```text
提高最大HP
+
直接恢复满血
```

因此不要总在满血时升级。

核心原则：

> 先让旧等级血量承担一部分伤害，再利用升级恢复满血。

即：

```text
可以买升级券
但暂时拿在背包中

等建筑实际受损
→ 再 use
```

这样升级券同时具有：

```text
升级
+
一次回血
```

的价值。

---

## 基地

例如：

```text
StationUpgradeVoucher 已购买
```

基地仍接近满血：

```text
暂缓使用
```

基地已经明显受伤：

```text
择机升级
```

但是设置安全底线：

```text
不能为了白嫖更多HP
冒基地被直接打爆的风险
```

所以存在：

```text
emergency_hp
```

一旦低于安全线：

```text
立即升级
```

---

## 围墙

同理。

优先升级：

```text
真正承担伤害的正面墙
```

满血且长期没挨打的墙：

```text
不急着升级
```

因此围墙升级顺序由：

```text
实际承伤
+
战略位置
```

决定，而不是简单按坐标顺序全部升。

---

# 十七、WallFixer / 重建 / 升级的选择

### Lv1普通墙

如果只是普通墙：

```text
残血
→ remove + 1 stone 重建
```

通常比花金币修复更划算。

### Lv1关键墙

如果本来就准备升级：

```text
残血
→ WallUpgradeVoucher1
```

同时获得升级 + 回血。

### Lv2墙

如果未来要 Lv3：

```text
残血较多
→ 优先用 WallUpgradeVoucher2
```

如果当前等级已经够：

```text
→ WallFixer
```

### Lv3墙

无法再升级：

```text
→ WallFixer
```

---

# 十八、世界新闻和卖矿

价格不要写死。

每天读取：

```text
vendorShopList
+
worldNews
```

如果新闻明确：

```text
某种矿明天停产
+
价格上涨
```

那么今天可以：

```text
多采该矿
```

并且不要机械全部卖掉。

卖矿策略：

```text
先兑现当天必须消费的钱
```

例如今天需要：

```text
200 gold
```

只卖到满足：

```text
MandatoryGold >= 200
```

如果某矿未来明确涨价：

```text
剩余库存可以留仓
```

---

# 十九、删除旧的固定经济规则

本次重构后，应尽量移除或弱化以下逻辑：

```text
DAY1_ORE_PHASE_ROUNDS
STONE_MORNING_ROUNDS
MIN_SELL_BATCH
固定 Round 返程
固定早上/下午采某矿
大量 DayN 专属 if
```

这些规则统一替换为：

```text
DailyPlan
StoneQuota
MineScore
TailCost
Deadline
```

---

# 二十、夜晚策略

夜晚不做复杂 Planner。

目标：

> 固定岗位、少移动、保持火力连续。

武器固定为：

```text
Rocket ×2
Railgun ×1
```

三个角色分工：

```text
角色 A：
双 Rocket 操作员

角色 B：
Railgun 主操作员

角色 C：
Support
```

---

# 二十一、双火箭

一个角色不能同一回合同时操作两座武器。

所以“双火箭操作员”的含义是：

```text
一个角色站在两座 Rocket 都可操作的位置
→ 根据 cooldown 轮流操作
```

理想炮位布局应尽量存在：

```text
一个共享站位
```

使角色 A 不需要在两座火箭之间来回移动。

每回合：

```text
读取 Rocket1.cooldown
读取 Rocket2.cooldown

只有一座 ready：
→ 使用该炮

两座都 ready：
→ fire_control 选择收益更高的一座

都 cooldown：
→ 暂时无火箭攻击
```

不要硬编码奇偶回合。

---

# 二十二、Railgun

角色 B：

```text
固定 Railgun 操作
```

原则：

```text
有有效目标
→ 尽量每回合 attack
```

目标选择继续交给 `fire_control.py`。

重点利用：

```text
Railgun穿透
```

最大化单次弹道收益。

---

# 二十三、Support

第三角色主要：

```text
围墙维修
基地 / 围墙升级
WallFixer
必要时补炮
必要时使用 Bomb / DizzyWeapon
```

默认尽量站在：

```text
关键墙
+
武器
```

都比较近的位置。

夜晚尽量少移动。

不要因为墙掉少量 HP 就立刻跑过去。

只有：

```text
关键墙进入危险区
```

才抢占普通辅助工作。

---

# 二十四、夜间防御原则

夜晚最高原则：

```text
不要频繁换岗位
不要因为远处暂时没机器人就提前离炮
不要轻易认为 battle_over
```

角色应该尽可能保持：

```text
Rocket Operator
Railgun Operator
Support
```

固定整晚。

如果机器人还存在：

```text
原则上继续保持防御岗位
```

不要因为基地附近暂时没有机器人就提前跑出去。

---

# 二十五、整体策略一句话总结

白天：

```text
先算今天必须完成什么
→ Pioneer做Task和采购
→ 两工人动态采矿
→ 优先采光矿触发刷新
→ Stone只采到建设需求
→ 铜铁负责经济
→ 按各角色TailCost动态决定何时停止采矿
→ 卖矿、采购、建造、升级
→ 全员回夜间岗位
```

夜晚：

```text
双Rocket + Railgun固定防守
→ 一人轮流双Rocket
→ 一人专职Railgun
→ 一人负责辅助、维修和升级兑现
→ 尽量不移动
→ 保证整晚操作连续性
```

---

# 二十六、本次 Codex 修改原则

本次更新重点是策略收敛，不继续扩架构。

要求：

```text
1. 保留现有稳定的寻路、协议、fire_control等基础能力。
2. 重写/简化白天 economy 与角色职责。
3. 删除大量固定回合经验规则。
4. DailyPlan成为当天战略来源。
5. Worker只负责采矿/施工。
6. Pioneer负责Task/采购/升级物流。
7. 夜晚保持固定岗位，不引入复杂任务竞争。
8. 所有行动必须受到夜晚返程deadline约束。
9. 任何优化不得破坏夜间三角色准时就位。
10. 新策略必须增加日志，能够明确解释：
   - 当天计划
   - StoneQuota
   - Gold目标
   - 为什么选择某个矿
   - 当前TailCost
   - 为什么停止采矿
   - Pioneer当前Task/采购状态
   - 为什么升级或暂缓升级某建筑
```

目标不是写出更复杂的策略，而是：

> 用少量稳定规则覆盖绝大多数比赛情况，让后续只需要调整战略参数和优先级，而不用继续修改整个调度结构。

这版可以直接给 Codex。建议让它先按这份说明做**策略重构，不顺手修改夜战判定、Task执行器和 fire_control**，避免又出现一次“改经济顺便把其他模块改坏”的情况。
