# sim-news-system 中立新闻系统规格

## Purpose

以独立 LLM 调用上下文按天生成《未来战争》的世界新闻（官方消息与民间传闻），并确定性计算新闻对矿石价格与采集的影响，为对战双方提供对称的推理素材。

## ADDED Requirements

### Requirement: 每日新闻生成
新闻系统 SHALL 在每个白天第一个回合产出一条官方消息与一条民间传闻，经独立 LLM 上下文（deepseek-v4.1-flash）生成，措辞风格与任务书示例一致（叙事化、含推理线索）。新闻生成 MUST 不携带对战双方的任何 prompt 历史与策略信息。

#### Scenario: 日常生成
- **WHEN** 比赛进入第 3 个白天的第一回合
- **THEN** worldNews 中出现第 3 天的官方消息与民间传闻

#### Scenario: 与agent上下文隔离
- **WHEN** 新闻系统调用 LLM 生成当日新闻
- **THEN** 该 LLM 调用的上下文中不包含我方/对手 agent 的任何 prompt 或 llmResp 内容

### Requirement: 官方消息与经济影响
新闻系统 SHALL 按可配置的事件模板（矿区塌方/矿脉发现/贸易政策等）生成官方消息，并输出确定性的影响参数：影响矿种、影响天数、价格倍率、可采性。当日 vendorShopList 价格 MUST 按该参数计算。官方消息文本 MUST 包含足够推断出这些影响的线索，但不直接给出数值。

#### Scenario: 塌方事件影响
- **WHEN** 生成"铁矿塌方、修复需 2 天"事件的当天与次日
- **THEN** 铁矿区不可采集，小贩铁收购价上涨；第 3 天起恢复原价与可采性

#### Scenario: 线索可推断
- **WHEN** 只读官方消息文本（不读内部参数）
- **THEN** 可以推断出受影响矿种与影响持续天数

### Requirement: 民间传闻与宝藏谜题
新闻系统 SHALL 维护一个全局唯一的宝藏谜题：宝藏地点、开启祭品组合（任务用品）、开启时间窗口，并在 N 天的民间传闻链中逐日释放线索，使坚持收集的玩家可以收敛出完整答案。谜题答案 MUST 在开局前确定性生成并锁定，不随对局中途调整。

#### Scenario: 线索链收敛
- **WHEN** 读齐从 DAY1 到 DAYN 的全部民间传闻
- **THEN** 可以唯一确定宝藏地点、所需祭品组合与开启时间

#### Scenario: 祭品错误消耗
- **WHEN** 开拓者以错误祭品组合执行 summonTreasure
- **THEN** lastSummonTreasureResult 返回 3，且祭品被消耗

#### Scenario: 正确召唤
- **WHEN** 开拓者在正确地点、正确时间、携带正确祭品组合执行 summonTreasure
- **THEN** lastSummonTreasureResult 返回 1，宝藏积分与金币发放，之后召唤返回 4
