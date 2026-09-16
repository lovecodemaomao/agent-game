# CoreGeek V2 架构契约

## 基线与边界

- 来源：`lovecodemaomao/agent-game-private`，`main`，基线 `6877a39fcdde8fb809c1cb99bf13dfdfcd06cfb6`，2026-09-15 核对远端。
- 规则唯一依据：仓库根目录 `docs/任务书.md`、`docs/接口文档.md`。用户策略是偏好，不能改变硬规则。`sim/`、旧策略和旧测试不是规则来源。
- 独立 Python 包 `coregeek_v2`。不导入旧 `agent`，不修改旧版。标准库运行，Python >= 3.11。
- 本次策略基线为 `780e98e42e0053d9828b9658e820c6a042cf82a6`。`Agent.production()` 注册正式 Planner / Job；`Agent()` 保持空配置用于架构注入测试，DummyJob 只在测试目录内。
- 两份用户说明原文保存在 `docs/REFACTOR_BRIEF.md` 和 `docs/DAY_NIGHT_STRATEGY.md`。业务策略已经接入；未接入真实 LLM 服务，辅助输出由平台执行。

## 固定回合流程

1. Parse：请求转换为 WorldState，验证回合、地图和角色 ID。
2. Feedback：上轮实际发出的动作 + 上轮世界快照 + 当前官方反馈生成 Outcome。
3. Update：GameMemoryReducer 记录事实；仅匹配当前 ACTIVE Job 和 Assignment 的 Outcome 交给该 Job。
4. Strategy：DailyStrategy 每个新游戏日生成窄 DayPlan；没有存活基地时由 Scheduler 取消任务、释放资源并返回完整空响应。
5. Maintain：终止完成/失败任务，检查角色、剩余耗时，协调未花预算，处理 Rebind。
6. Propose：业务 Planner 提出候选，不拿可写运行态。
7. Preempt / Assign：按优先级、收益、semantic_key 排序；优先空闲角色，必要时明确抢占。替换与资源预留在同一事务内完成。
8. Intent：纯火控为已分配的夜间操作员组合武器目标；当前 Job 产生零或一个 Intent。
9. Movement：统一导航，解析一步移动与己方冲突。
10. Execute：Executor 生成带来源的 CommandEnvelope 列表。
11. Validate：在构造映射前检查重复角色/武器、所有权、预留预算和基本协议；非法命令不能靠字典覆盖消失。
12. Record：仅记录验证后实际发出的动作及不可变世界快照。
13. Response：Executor 构造命令映射；ResponseBuilder 合并独立 TaskExecution 输出。

整个回合在锁内对 Session 副本执行。全部成功后才提交 Session；同队伍同回合直接返回已缓存响应的副本。回合回退重建该队伍 Session，不影响其他队伍。服务器协议没有独立 match ID，因此同队伍同回合的新局无法与重试区分；比赛正常重启进程或回合回退即可重置。

## 字段所有权

| 数据 | 唯一写者 | 约束 |
| --- | --- | --- |
| WorldState / Role / payload | WorldParser | frozen dataclass、FrozenMap、tuple；嵌套数据也不可变 |
| GameMemory | GameMemoryReducer | 保存历史 Outcome，不缓存“当前金币/位置/背包” |
| DayPlan | StrategicPlanner | 只描述目标和政策，不保存角色任务分配 |
| RuntimeState.jobs / assignments / sequence | Scheduler | 每角色最多一个 ACTIVE Job |
| JobRecord.lifecycle / binding | Scheduler | 不可变 Record 由 Scheduler 替换；Job 对象不持有 Record |
| Job.stage | 对应 Job | 仅执行进度；通过 Signal 提出终止/重绑定请求 |
| RuntimeState.reservations | ReservationManager | 每条 Reservation 显式包含 owner_job_id；账本 key 必须一致 |
| RuntimeState.tombstones | Scheduler | 保存抢占原因和冷却窗口 |
| RuntimeState.previous_actions / previous_world | Recorder | 仅保存最终输出，供下一轮反馈使用 |
| Intent | 当前 Job | MoveIntent 或 ActionIntent，不是协议命令 |
| 解析后的一步移动 | MovementCoordinator | 不改变业务目标 |
| CommandEnvelope / roleCommandMap | Executor | 其他模块不得生成协议命令映射 |
| Outcome | OutcomeBuilder | 原任务 ID、角色 ID、回合和 Stage 可追溯 |
| Session 缓存与事务提交 | Agent | 按队伍隔离、锁内提交 |

Python 层通过不可变数据、窄参数和测试约束职责；不是对恶意插件的安全沙箱。

## 关键接口

- `Planner.propose(world, memory_view, day_plan, reservations_view) -> Iterable[JobProposal]`：history 为不可变 Outcome 元组，预留账本为只读映射。可用金币由真实金币减所有未花预留计算。
- `JobProposal`：业务种类、semantic_key、合格角色、来源、优先级、收益、绑定目标、绝对 deadline、初始总耗时估计、资源请求、可中断标记和备选目标。`deadline` 使用从 1 开始的绝对游戏回合。
- `Job.on_outcome(outcome) -> Signal`、`Job.check(world, role_id, binding) -> Signal`。支持 CONTINUE / SUCCESS / FAIL / NEEDS_REBIND / RETRY；RETRY 保持任务，重试策略由具体 Job 负责。
- `Job.remaining_duration(world, role_id, binding, navigation)`：剩余完整事务链耗时。默认实现仅用于到达某可站立终点。矿、建筑等不可站立 binding 必须由业务 Job 计算交互邻格、操作回合及最终返程；不能用“走到矿格”代替采矿行程。
- `Job.reservation_request(...)`：申请剩余未花预算。收到可靠采购结果后减少预留；背包中的券由 WorldState 表示，不再占已花金币。普通预算更新不能修改排他键，绑定变更只走 Rebind。
- `Navigation.path/cost/itinerary_cost`：同一八方向 BFS，单位边权，允许绕障和对角穿过两个障碍之间；不可达为 `None` / `inf`。起终点是可站立格。此版本按当前占用保守估计，未实现时空路径或拥堵预测。
- `MoveIntent(goal)` 经协调后成为一步移动；`ActionIntent('move', ...)` 被拒绝，防止绕过碰撞处理。`ActionIntent` 其他参数按官方协议表达；攻击额外使用内部 `_weapon_id` 指定武器，Executor 转成武器 ID 映射键和角色 `controllerId`。
- `TaskExecution.output() -> (prompt, executeCmd)` 优先承接 ACTIVE TaskJob 的输出，否则处理有证据的新闻分析。TaskJob 以 job_id、回合及 request_id 关联 LLM 回填；命令结果仅消费紧邻上一回合的 pending 请求。没有本地 shell/LLM 调用。

## 调度、资源与反馈约束

- Lifecycle 支持 CREATED、ACTIVE、COMPLETED、FAILED、PREEMPTED、CANCELLED。创建与激活在一次原子分配内完成，因此 CREATED 不跨回合驻留。所有终态释放 Assignment 和 Reservation；不实现 suspend/resume。
- 优先级：EMERGENCY > NIGHT_PREP > CRITICAL_DEFENSE > NORMAL > OPTIONAL。同级不抢占；EMERGENCY 可抢占较低级可中断任务，NIGHT_PREP/CRITICAL_DEFENSE 只抢 NORMAL/OPTIONAL。任意任务都不能抢不可中断任务。
- 同 semantic_key 的 ACTIVE Job 全局去重。PREEMPTED 对同角色同 semantic_key 冷却到当天最后回合（含夜晚），次日可重新提案。
- 资源账本支持金币及任意可哈希排他键，如矿点、墙、建筑、升级、夜间岗位。Pos binding 自动附加排他键。预留先检查，抢占/替换一起提交，不会因新任务无钱而先取消旧任务。
- Maintain 先处理所有未花预算的减少，再按低优先级释放无法维持的任务；最后处理增加预算和 Rebind，避免刚买完物品造成错误的“双重计费”。不通过修改 world.gold 记账。
- Rebind 仅尝试 Proposal 声明的备选目标，重新检查剩余行程、deadline 和排他资源；成功才替换目标和预留，失败终止。保留已减少的预算，不重新预留已花的钱。
- 反馈 boolean 只代表合法性，不等于成功。移动核对位置；采集／买卖／用券核对背包变化；施工／拆除核对建筑变化；宝藏使用官方结果码。提交答案需前一轮存在任务、当前任务结束、动作合法且无错误，否则不能记成功方法。
- 跳过回合时不把当前 lastRound 结果错配给更早的指令，记录 UNCONFIRMED。终态任务的迟到结果仍进入历史，但不能推进新任务。
- 原始 LLM/沙盒回填留在 Outcome 证据中；TaskJob 独立管理 pending、探测历史和答案，GameMemoryReducer 仅在确认任务成功后记学习结果。旧任务回填不得驱动新任务。

## 业务依赖与运行边界

开发模式由 `Agent(development=True)` 开启，架构/命令错误暴露异常。默认比赛模式记录错误并丢弃单条非法命令；整个回合异常时返回完整空响应且不提交部分状态。无效命令不进入 PreviousActionRecord。

Validator 覆盖动作类别、角色权限、昼夜、参数与数量、相邻交互、建造圈与武器上限、移动占用、背包／预算、武器冷却／射程／目标数与加特林锥角。它不是完整判题器，不预测敌方同时动作与隐藏结果。Demo 没有购买／使用召唤令的生产策略。

移动协调阻止同格争抢、换位和进入本回合开始时有人占用的格子；本回合建造格预先加入移动障碍，不假设服务端动作顺序。岗位选择检查其他两人已经站岗后的通路；返程成本计入计划中的墙与岗位占用。交互站位必须有建成后的退出通路；造墙不能切断其他己方角色到岗位的路线。基地交互覆盖完整 2×2 占地。移动失败会暂时避开失败格；回防／夜防失败后继续重试，不进入普通任务失败冷却。未实现任意动态堵塞的时空导航，未知敌人和未来矿点刷新仍可能导致临时绕路或迟到。

模块依赖：`world/model` 定义状态和契约；`strategy` 只产 DayPlan / Proposal；`scheduler` 维护控制权和预留；`jobs/task_solver` 推进阶段并产 Intent；`combat/fire_control` 只为既有操作员选择武器和目标；`navigation` 处理路线／一步冲突；`execution` 唯一产命令；`feedback` 关联证据；`runtime/server` 负责事务与协议入口。

石料配额包括待建墙、普通 Lv1 墙拆建和一块备用石；Day2 起不把 Worker A 的石料计为 Worker B 可用库存。受损正面四墙视为关键墙，其他受损 Lv1 墙优先拆建。采购预留每轮按当前价格与背包缺口更新；任务完成、失败、取消和抢占均释放。Pioneer 同级候选按任务、已有物品交付、采购、待命排序；普通收益变化不打断已经进行中的任务。

日志贯穿 Proposal / ASSIGN → Job / Stage → Intent → Command → Outcome，并记录抢占、终止、资源不足、延后用券和回程预算。复现问题应保留连续 Payload 与标准错误日志。会话不落盘；比赛模式返回空响应不等于策略任务成功。
