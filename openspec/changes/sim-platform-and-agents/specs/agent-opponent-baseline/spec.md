# agent-opponent-baseline 对手基线 agent 规格

## Purpose

作为本地对战的对手方：与正式接口协议完全一致接入，10 天执行一种固定策略（可配置预设卡），不进行每日 LLM 策略调整；仅在任务与自进化任务期间使用 LLM 辅助，为策略对比提供稳定基线。

## ADDED Requirements

### Requirement: 固定策略执行
对手 agent SHALL 在整场比赛（10 天）内按单一预设策略卡执行（如龟缩防御流/经济流/骚扰流等，运行前选定），全程不发起每日策略 LLM 调用；策略卡 MUST 以参数文件形式可替换，行为确定性可复现（相同种子与输入下指令序列一致）。

#### Scenario: 策略卡切换
- **WHEN** 配置对手为"龟缩防御"策略卡后开赛
- **THEN** 对手整场按该卡行为执行，无每日策略变更

#### Scenario: 行为确定性
- **WHEN** 相同策略卡与相同对局输入重放两遍
- **THEN** 对手提交的指令序列一致

### Requirement: 每日零策略 LLM
对手 agent SHALL 在非任务期不消耗任何 LLM 调用（prompt 字段保持空），response 的每日 3 次配额对其而言形同虚设；对手 agent MUST 依赖与判题循环的接口交互（如新闻文本）也不做 LLM 分析，仅做规则化响应（或忽略）。

#### Scenario: 非任务期零调用
- **WHEN** 整场比赛的对手 response 被审计
- **THEN** 非任务期的 prompt 字段全部为空字符串

### Requirement: 任务期 LLM 辅助
对手 agent SHALL 在开拓者执行自进化任务期间使用 LLM 与沙盒（与公共骨架任务求解代理一致）完成探索与提交，以保证任务线的公平基线；任务结束后恢复零 LLM 调用。

#### Scenario: 任务期调用
- **WHEN** 对手开拓者领取自进化任务
- **THEN** 对手通过 prompt/executeCmd 通道推进任务求解直至 submitAnswer

### Requirement: 出站合规
对手 agent 的全部输出 SHALL 经公共骨架的严格格式校验器后出站，与 agent-framework 的校验要求完全一致——对手同样不得出现"指令错误"级别非法报文。

#### Scenario: 对手零非法报文
- **WHEN** 对整场比赛对手 response 做协议检查
- **THEN** 不存在指令字段缺失/动作码非法等 errorCode=4 类错误
