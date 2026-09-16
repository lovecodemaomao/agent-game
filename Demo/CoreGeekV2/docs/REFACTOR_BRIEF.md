下面这版可以直接交给 Codex，当作 **CoreGeek V2 重构任务书** 使用。我把前面讨论中过细、容易发散的内容收掉了，只保留真正会影响实现质量和避免架构 bug 的部分。

# CoreGeek V2 最终重构说明

## 0. 任务目标

当前 `Demo/CoreGeek` 已经出现明显的架构性问题：

* 多个模块可能同时控制同一个角色。
* 调用顺序隐式决定业务优先级。
* 世界真实状态、历史记忆、计划状态混杂。
* 采矿、建造、升级、采购、任务、回防等逻辑互相侵入。
* 路径计算存在多个实现和不同的“距离/可达”定义。
* 一个角色正在执行某任务时，可能被另外的模块中途重新指导。
* 白天结束、夜间回防、角色炮位等缺乏统一调度。
* 单个函数通常看起来没有问题，但多个模块组合后容易产生不可预测行为。

本次目标不是继续修补旧架构，而是建立一个新的统一调度系统 `CoreGeekV2`。

核心原则：

```text
Planner 决定“有什么值得做”
Scheduler 决定“谁去做”
Job 决定“这个角色持续怎么做”
Navigation 决定“怎么过去”
Executor 决定“这一回合发什么”
```

任何模块不得越权。

---

# 1. 重构方式

不要直接大规模修改旧 `Demo/CoreGeek`。

新建：

```text
Demo/CoreGeekV2/
```

旧代码保留作为：

```text
baseline
规则参考
策略参考
回归行为对照
```

可以复用成熟逻辑，但不要把旧的控制流程原样复制过来。

尤其禁止把旧的：

```text
brain.py
economy.py
tasks.py
```

整块复制后继续堆条件。

---

# 2. 官方规则来源

所有游戏规则以：

```text
docs/任务书.md
docs/接口文档.md
```

为唯一正式依据。

不要从：

```text
sim/
旧代码行为
现有测试中的错误假设
```

反推官方规则。

需要特别遵守：

```text
白天 = 70 回合
夜晚 = 60 回合
一天 = 130 回合
```

机器人在夜晚第一回合出现。

因此：

> 白天第 70 回合结束时，夜战所需角色应已经处于可立即防守的位置。

不要沿用旧代码中的：

```text
RETURN_DEADLINE = 75
```

之类逻辑。

---

# 3. V2 总体数据流

系统固定采用：

```text
Payload
   ↓
WorldParser
   ↓
WorldState
   ↓
Outcome / Feedback
   ↓
GameMemory Update
   ↓
StrategicPlanner
   ↓
DayPlan
   ↓
Business Planners
   ↓
JobProposal[]
   ↓
Scheduler
   ↓
RoleAssignment
   ↓
Active Job
   ↓
Job Stage
   ↓
Intent
   ↓
Navigation / MovementCoordinator
   ↓
Executor
   ↓
Validator
   ↓
ResponseBuilder
   ↓
Response
```

不允许业务模块绕过 Scheduler 或 Executor 直接控制角色。

---

# 4. 三种状态必须严格分离

## 4.1 WorldState

表示：

> 当前回合服务器告诉我们的真实世界。

例如：

```python
WorldState:
    round_no
    day_no
    phase
    phase_round

    gold

    roles
    buildings
    walls

    mines
    vendor
    weapon_shop
    task_points

    robots
    enemies

    vendor_prices
    weapon_shop_prices

    phase_task
    world_news

    last_round_role_action_results
    last_summon_treasure_result
    llm_resp
    last_cmd_result
    errors
```

要求：

```text
WorldState 创建后本回合绝对只读。
```

禁止：

```python
world.gold -= 100
world.worker.position = ...
```

所有业务模块只能读取 WorldState。

---

## 4.2 GameMemory

表示：

> 不能从当前 Payload 直接恢复的长期历史知识。

例如：

```python
GameMemory:
    night_reports
    mine_history
    task_history
    failure_history
    learned_task_information
    metrics
```

不要在 GameMemory 中重复保存：

```text
当前金币
当前位置
当前背包
当前墙血量
当前建筑等级
```

这些全部从 WorldState 读取。

---

## 4.3 RuntimeState

表示：

> 调度系统当前正在做什么。

例如：

```python
RuntimeState:
    jobs
    assignments
    reservations
    tombstones
    previous_actions
    current_day_plan
```

严格区分：

```text
WorldState   = 世界现在是什么
GameMemory   = 过去发生过什么
RuntimeState = 我们现在准备做什么
```

---

# 5. 最重要约束：一个角色只有一个控制者

RuntimeState 中统一保存：

```python
assignments = {
    worker1_id: job_id,
    worker2_id: job_id,
    pioneer_id: job_id,
}
```

系统必须保证：

```text
一个角色同一时间最多一个 ACTIVE Job。
```

以后禁止出现：

```python
if role not in commands:
    ...
```

这类通过代码执行顺序争夺角色控制权的模式。

业务模块不能：

```python
move(worker)
collect(worker)
build(worker)
```

只能提交：

```python
JobProposal(...)
```

Scheduler 是唯一角色任务分配者。

---

# 6. Planner 的职责

Planner 只负责回答：

> “现在有什么事情值得做？”

建议保留以下业务 Planner：

```text
EconomyPlanner
ConstructionPlanner
DefensePlanner
TaskPlanner
TreasurePlanner
NightPrepPlanner
```

另外有：

```text
StrategicPlanner
```

负责生成 DayPlan。

Planner 输入：

```text
WorldState
GameMemory
DayPlan
```

输出：

```text
JobProposal[]
```

Planner 可以决定：

```text
哪个矿值得采
需要多少石头
哪堵墙值得修
哪座武器应该升级
任务值不值得做
宝藏是否值得尝试
什么时候进入 NightPrep
```

Planner 不允许决定：

```text
当前角色直接走哪一格
最终谁执行某 Proposal
取消其他角色的 Job
修改 Reservation
直接生成 roleCommandMap
```

---

# 7. JobProposal

建议统一结构：

```python
JobProposal:
    kind
    semantic_key

    eligible_roles

    priority_class
    utility

    target
    deadline

    estimated_duration
    estimated_cost

    reservation_request

    interruptible

    fallback_targets
```

例如：

```python
JobProposal(
    kind="MineTrip",
    semantic_key="mine:copper:17",

    eligible_roles=[worker1, worker2],

    priority_class=NORMAL,
    utility=18.2,

    target=mine17,
    deadline=60,
)
```

`semantic_key` 用于识别：

```text
这个 Proposal 是否与已有 ACTIVE Job 相同
是否正在 cooldown
是否已经被其他 Job 占用
```

不要让每回合重复生成的同一任务造成重复调度。

---

# 8. Scheduler 必须保持简单

Scheduler 只负责：

```text
1. Maintain
2. Preempt
3. Assign
```

不要让 Scheduler 变成新的 `brain.py`。

---

## 8.1 Maintain

检查已有 ACTIVE Job：

```text
是否完成
是否失败
目标是否仍有效
是否仍能在 deadline 前完成
是否仍满足 Reservation
是否需要 rebind
```

正常：

```text
继续原 Job
```

不要因为每回合出现稍微更高收益的 Proposal 就改变任务。

---

## 8.2 Preempt

只有明确更高优先级的任务才能抢占。

建议优先级：

```text
EMERGENCY
NIGHT_PREP
CRITICAL_DEFENSE
NORMAL
OPTIONAL
```

基本规则：

```text
EMERGENCY 可抢占所有可中断 Job。

NIGHT_PREP
可以抢 NORMAL / OPTIONAL。

CRITICAL_DEFENSE
可以抢 NORMAL / OPTIONAL。

同级默认不抢。

普通收益提升禁止抢占。
```

例如：

```text
另一个矿多赚 2 金币
```

绝不能让工人立即调头。

---

## 8.3 Assign

只给：

```text
当前没有 ACTIVE Job 的角色
```

分配新 Job。

Scheduler 不负责计算：

```text
哪个矿收益最高
哪堵墙最危险
升级哪个建筑
```

这些必须已经由 Planner 算好。

---

# 9. Job 采用完整业务闭环

不要把一个业务行为拆成大量短 Job。

错误示例：

```text
GoMineJob
CollectJob
SellJob
ReturnJob
```

应采用：

```text
MineTripJob
```

内部状态：

```text
GO_MINE
COLLECT
GO_VENDOR
SELL
RETURN
DONE
```

原则：

> Job 是一次业务承诺，Stage 是执行进度。

例如：

### MineTripJob

```text
GO_MINE
COLLECT
GO_VENDOR
SELL
RETURN
```

### BuildWallJob

```text
PREPARE_RESOURCE
GO_BUILD_POSITION
BUILD
RETURN
```

### UpgradeBuildingJob

```text
GO_SHOP
BUY
GO_TARGET
USE
RETURN
```

### TaskJob

```text
GO_TASK_POINT
ACCEPT
EXECUTE
SUBMIT
RETURN
```

---

# 10. Job Lifecycle

V2 第一版只支持：

```text
CREATED
ACTIVE

COMPLETED
FAILED
PREEMPTED
CANCELLED
```

其中：

```text
COMPLETED
FAILED
PREEMPTED
CANCELLED
```

全部是终态。

第一版不要实现通用：

```text
SUSPENDED
RESUME
```

被抢占后：

```text
旧 Job → PREEMPTED
```

如果之后仍值得继续：

```text
Planner 再生成新 Proposal
Scheduler 创建新 Job
```

避免暂停恢复导致架构复杂化。

---

# 11. Job 不允许自己改变 lifecycle

Job 可以修改：

```text
自己的 Stage
```

Job 不允许自己：

```text
COMPLETED
FAILED
PREEMPTED
```

Job 通过 Signal 告诉 Scheduler：

```text
CONTINUE
SUCCESS
FAIL
NEEDS_REBIND
RETRY
```

例如：

```text
Outcome
↓
Job.on_outcome()
↓
JobSignal.SUCCESS
↓
Scheduler Maintain
↓
lifecycle = COMPLETED
↓
释放 Assignment 和 Reservation
```

这样避免 Job 和 Scheduler 对生命周期产生不同理解。

---

# 12. Rebind 和 Fallback

Job 的业务目标默认不可改变。

例如：

```text
采铜 → 卖铜 → 回防
```

Job 不允许自己决定变成：

```text
采铁
建墙
采购升级券
```

允许两类变化。

---

## 12.1 Execution fallback

Job 可以自己处理：

```text
换路线
换相邻格
绕开临时障碍
```

不改变业务目标。

---

## 12.2 Binding fallback

例如：

```text
铜矿 A 消失
```

Proposal 创建时可带：

```text
fallback_targets = [铜矿B, 铜矿C]
```

Job 提交：

```text
RebindRequest
```

Scheduler 检查：

```text
Feasibility
Reservation
deadline
```

批准后修改 binding。

否则 Job FAILED。

Job 不允许自行重新选择业务方向。

---

# 13. Preempted Tombstone

为避免抢占抖动，PREEMPTED 后保留：

```python
PreemptedTombstone:
    semantic_key
    role_id

    reason
    round_no
    cooldown_until
```

例如：

```text
MineTrip copper mine17
被 NightPrep 抢占
```

当天不要下一回合又创建同样的 MineTrip。

目的：

```text
避免：
采矿
→ 抢占
→ 又采矿
→ 又抢占
→ 无限抖动
```

---

# 14. ReservationBook

所有资源冲突统一通过：

```text
ReservationBook
```

管理。

至少包括：

```text
金币
矿点
围墙目标
建筑目标
升级目标
夜间岗位
```

例如：

```text
真实金币 = 130
WeaponUpgradeJob 已预留 100

available_gold = 30
```

业务模块不能直接根据：

```text
world.gold == 130
```

就认为还有 130 可用。

---

## Reservation 原则

```text
Planner：只读
Job：只读/提出请求
Scheduler：提交或释放
ReservationManager：唯一修改账本
```

任何 Reservation 必须有：

```text
owner_job_id
```

开发期要求：

```python
assert reservation.owner_job_id in active_jobs
```

禁止孤儿 Reservation。

---

# 15. Navigation 统一

所有业务模块禁止实现自己的真实寻路。

统一提供：

```python
navigation.path(...)
navigation.cost(...)
navigation.itinerary_cost(...)
```

例如采矿：

```text
当前位置
→ 矿
→ Vendor
→ NightPost
```

必须使用同一 Navigation 计算。

官方移动规则：

```text
8 方向移动
切比雪夫距离
障碍物包含建筑、角色、机器人、中立单位、矿区、任务点等
```

MovementCoordinator 还需要处理：

```text
两个己方角色争夺同一格
两个角色互换位置
一个角色走进另一个未移动角色位置
```

不要让各业务模块分别处理碰撞。

---

# 16. Intent 保持非常薄

Job 不直接生成最终协议 Command。

Job 输出：

```python
MoveIntent(goal=...)
```

或：

```python
ActionIntent(
    action="collect",
    target=...
)
```

MovementCoordinator：

```text
只解决移动和空间冲突。
```

Executor：

```text
把最终 Intent 翻译成接口 Command。
```

不要在 Intent 中再建立一套复杂状态机。

---

# 17. Executor 是唯一 Command 写者

整个项目只有 Executor 可以生成：

```text
roleCommandMap
```

因此必须保证：

```text
一个角色每回合最多一个 Command。
```

Planner、Scheduler、Job、Navigation 都禁止直接修改：

```text
roleCommandMap
```

---

# 18. Task Response 是独立输出

官方 Response 不只有：

```text
roleCommandMap
```

还有：

```text
prompt
executeCmd
```

因此建议：

```text
Executor
├── RoleCommandExecutor
└── TaskExecution
```

最后：

```text
ResponseBuilder
```

统一生成：

```python
{
    "roleCommandMap": ...,
    "prompt": ...,
    "executeCmd": ...
}
```

不要强行把 `prompt / executeCmd` 塞进普通角色 Command 系统。

---

# 19. Outcome / Feedback

上一回合发出去的动作需要保存：

```python
PreviousActionRecord:
    round_no
    role_id

    job_id
    job_stage

    command
```

下一回合结合：

```text
上一回合 WorldState
上一回合 Command
当前 WorldState
lastRoundRoleActionResults
lastSummonTreasureResult
llmResp
lastCmdResult
errors
```

生成 Outcome。

例如：

```text
MOVE_SUCCESS
MOVE_BLOCKED

COLLECT_SUCCESS
COLLECT_FAILED

BUY_SUCCESS
BUY_FAILED

BUILD_SUCCESS
BUILD_FAILED

TREASURE_SUCCESS
TREASURE_WRONG_ITEMS
```

---

## Outcome 有两个消费者

### GameMemory

永远记录真实发生的结果。

即使原 Job 已经：

```text
PREEMPTED
FAILED
CANCELLED
```

历史事实仍然应该记录。

---

### Job

只有满足：

```text
Outcome.origin_job_id == 当前 ACTIVE Job.id
```

才允许推进该 Job 的 Stage。

禁止旧 Job 的 Outcome 影响新的 Job。

---

# 20. DayPlan

StrategicPlanner 输出一个窄的：

```python
DayPlan:
    version

    stone_target
    gold_reserve

    defense_pressure

    economy_priority
    task_priority

    upgrade_policy
    night_prep_policy
```

DayPlan 不允许保存：

```text
Worker1 去矿
Worker2 去建墙
Pioneer 去商店
```

这是 Scheduler 的职责。

---

# 21. NightPrep

NightPrep 必须成为明确的高优先级 Planner。

目标：

> 白天第 70 回合结束前，完成夜战准备。

NightPrep 负责提出：

```text
回到夜间岗位
关键维修
关键升级
关键物资交付
```

每个角色需要有：

```text
NightPost
```

例如：

```text
Worker1 → Rocket seat
Worker2 → Rocket seat
Pioneer → Railgun seat
```

具体配置后续由战略策略决定。

但所有经济 Job 的返回终点应该是：

```text
assigned NightPost
```

而不是笼统的：

```text
home
nearest weapon
station
```

---

# 22. Economy Planner

EconomyPlanner 负责：

```text
采什么矿
采多少
什么时候卖
哪个 Vendor
一次完整 Trip 是否赚钱且来得及
```

MineTrip 应计算完整行程：

```text
当前位置
→ Mine
→ Collect
→ Vendor
→ Sell
→ NightPost
```

而不是只计算：

```text
当前位置 → Mine
```

石头也是合法可出售资源。

“通常不卖石头”属于策略，而不是 Rules。

---

# 23. Construction Planner

负责：

```text
建武器
建围墙
必要时拆墙
```

不负责直接控制 Worker。

输出：

```text
BuildWeapon Proposal
BuildWall Proposal
RemoveWall Proposal
```

由 Scheduler 选择哪个 Worker 执行。

---

# 24. Defense Planner

统一负责：

```text
武器升级
围墙升级
基地升级
围墙维修
防御消耗品
```

不要把：

```text
RepairPlanner
UpgradePlanner
```

做成互相独立且会竞争同一建筑的两个系统。

官方规则中：

```text
建筑升级后会恢复满血
```

因此升级和维修必须统一比较价值。

---

# 25. Task Planner

Pioneer 自进化任务必须作为强独占 Job。

官方规则：

```text
任务期间 Pioneer 离开己方任务点周围一格
会导致任务结束。
```

因此：

```text
TaskJob ACTIVE
```

期间其他普通 Planner 不允许直接拉走 Pioneer。

如果必须回防：

```text
Scheduler 明确 PREEMPT TaskJob
```

而不是 Shop 或 Night 逻辑直接覆盖 Pioneer Command。

---

# 26. Treasure Planner

Treasure 独立于普通 Task。

`summonTreasure` 使用官方：

```text
lastSummonTreasureResult
```

判断结果。

不要通过背包变化猜测 Treasure 是否成功。

---

# 27. Rules 层

所有硬规则集中在：

```text
rules.py
```

例如：

```python
DAY_ROUNDS = 70
NIGHT_ROUNDS = 60

can_move(...)
can_collect(...)
can_build(...)
can_attack(...)
can_accept_task(...)
can_submit_answer(...)
can_summon_treasure(...)
```

不要在 Economy、Task、Construction 中各自复制官方规则判断。

---

# 28. Validator

最终 Command 输出前必须经过 Validator。

检查：

```text
一个角色是否有多个 Command
角色是否有权限执行动作
当前昼夜是否允许动作
目标参数是否缺失
targetPos 数量是否正确
是否违反基本协议
```

开发模式：

```text
违反架构 invariant 直接 assert。
```

比赛模式：

```text
记录错误
丢弃非法命令
使用保守降级
```

避免整个服务因为一个断言直接退出。

---

# 29. 固定回合生命周期

整个程序统一按照：

```text
1. Parse
   Payload → WorldState

2. Feedback
   根据上一回合 Action 和世界变化生成 Outcome

3. Update
   Outcome 更新 GameMemory
   Outcome 推进对应 Job Stage

4. Strategy
   必要时更新 DayPlan

5. Maintain
   检查现有 ACTIVE Job

6. Propose
   Planners 生成 JobProposal

7. Preempt
   处理高优先级抢占

8. Assign
   给空闲角色分 Job

9. Intent
   每个 ACTIVE Job 生成 Intent

10. Movement
    解决移动冲突

11. Execute
    Intent → Command

12. Validate
    校验最终 Command

13. Record
    保存本轮 Action 和 World snapshot

14. Response
```

不允许某个业务模块绕过这个生命周期额外控制角色。

---

# 30. 字段所有权

必须在 `ARCHITECTURE.md` 明确写出：

```text
WorldState
创建：WorldParser
之后只读

GameMemory
写：GameMemoryReducer

DayPlan
写：StrategicPlanner

RuntimeState.jobs
写：Scheduler

RuntimeState.assignments
写：Scheduler

RuntimeState.reservations
写：ReservationManager

RuntimeState.tombstones
写：Scheduler

PreviousActionRecord
写：Recorder

Job.lifecycle
写：Scheduler

Job.stage
写：Job

Job.binding
写：Scheduler

Intent
写：当前 Job

MovementDecision
写：MovementCoordinator

Command
写：Executor

Outcome
写：OutcomeBuilder
```

这是整个 V2 最重要的架构契约之一。

---

# 31. 推荐目录

建议：

```text
Demo/CoreGeekV2/
│
├── main3.py
│
└── src/agent/
    │
    ├── core/
    │   ├── world.py
    │   ├── state.py
    │   └── rules.py
    │
    ├── planning/
    │   ├── strategy.py
    │   ├── proposals.py
    │   └── scheduler.py
    │
    ├── planners/
    │   ├── economy.py
    │   ├── construction.py
    │   ├── defense.py
    │   ├── tasks.py
    │   ├── treasure.py
    │   └── night_prep.py
    │
    ├── jobs/
    │   ├── base.py
    │   ├── economy.py
    │   ├── construction.py
    │   ├── defense.py
    │   └── tasks.py
    │
    ├── navigation/
    │   ├── pathfinder.py
    │   └── movement.py
    │
    ├── feedback/
    │   └── outcome.py
    │
    ├── execution/
    │   ├── executor.py
    │   ├── validator.py
    │   └── response.py
    │
    └── combat/
        └── fire_control.py
```

不要为了模式漂亮继续拆更多层。

---

# 32. 实施顺序

不要一次把旧功能全部搬入 V2。

## Phase 1：架构骨架

先完成：

```text
WorldState
GameMemory
RuntimeState
Job
JobProposal
Scheduler
Outcome
Executor
Validator
```

只使用非常简单的测试 Job。

目标：

```text
角色控制权稳定。
```

---

## Phase 2：Navigation

完成：

```text
统一寻路
MovementCoordinator
碰撞处理
```

目标：

```text
三个角色不会因为自己的调度互相卡死。
```

---

## Phase 3：Worker 基础功能

依次接入：

```text
BuildWeaponJob
BuildWallJob
MineTripJob
```

先让 Worker 系统稳定。

---

## Phase 4：经济与防御投资

加入：

```text
ReservationBook
采购
升级
维修
动态矿点评分
预算
```

---

## Phase 5：Pioneer

加入：

```text
TaskJob
TreasureJob
任务用品采购
```

---

## Phase 6：NightPrep

加入：

```text
最终回防
夜间岗位
关键升级
关键维修
```

保证：

```text
第 70 回合结束前 NightReady。
```

---

## Phase 7：接入夜战

尽量复用当前成熟 FireControl。

边界：

```text
Scheduler：
谁操作哪座武器。

FireControl：
这座武器攻击谁。
```

FireControl 不负责白天调度和角色回防。

---

# 33. 第一阶段必须写的架构测试

至少实现：

```text
test_one_role_one_active_job

test_one_role_one_command_per_round

test_planner_cannot_directly_control_role

test_job_persists_across_rounds

test_completed_job_releases_assignment

test_failed_job_releases_assignment

test_preempted_job_releases_reservation

test_no_orphan_reservations

test_old_outcome_does_not_update_new_job

test_same_priority_job_does_not_preempt

test_night_prep_can_preempt_economy

test_task_job_keeps_pioneer_locked

test_workers_do_not_move_into_same_cell

test_no_role_position_swap_collision

test_day_70_night_ready
```

---

# 34. 第一阶段不要追求策略收益

最初验收不要看：

```text
金币赚多少
矿采多少
得分多少
```

只看架构：

```text
1. 一个角色从未出现两个 ACTIVE Job。
2. 一个角色从未生成两条 Command。
3. Planner 从未直接控制角色。
4. Job 不会无故被另一个业务模块覆盖。
5. Job 结束一定释放角色。
6. Reservation 没有孤儿。
7. Pioneer 做任务期间不会突然去商店。
8. NightPrep 可以正常抢占普通经济任务。
9. 所有行为都可以追溯：
   Proposal
   → Job
   → Intent
   → Command
   → Outcome
```

架构稳定后再优化策略。

---

# 35. 日志要求

每个角色每回合建议至少输出：

```text
round
role_id

job_id
job_kind
job_stage

proposal_source

intent
command

outcome_from_previous_round

lifecycle_change
```

Scheduler 发生变化时额外记录：

```text
ASSIGN
PREEMPT
COMPLETE
FAIL
CANCEL
REBIND
```

要求看到日志后能够回答：

> 为什么这个角色这一回合会走到这里？

---

# 36. 不要做的事情

本轮 V2 明确禁止：

```text
不要原地重构旧 CoreGeek。
不要整块复制旧 brain.py。
不要让 Scheduler 变成新的巨型 brain。
不要让 Planner 互相调用。
不要让 Planner 直接生成 Command。
不要让 Job 自己抢占 Job。
不要让不同模块实现不同路径系统。
不要实现通用 suspend/resume。
不要为了架构漂亮创建大量 Factory / Manager。
不要一开始同时迁移所有复杂策略。
```

---

# 37. 最终设计原则

整个 V2 必须始终满足：

```text
Planner：
有什么值得做？

Scheduler：
谁做？

Job：
这个人如何持续完成这件事？

Navigation：
怎么到达？

Executor：
这一回合具体发什么？

Outcome：
上一回合到底发生了什么？
```

任何模块只回答自己的问题。

一旦某个模块开始回答别人的问题，就说明职责边界正在再次腐化。

---

# 38. 本次 Codex 首轮任务

首轮不要实现全部游戏策略。

请优先：

```text
1. 创建 Demo/CoreGeekV2。
2. 写 ARCHITECTURE.md。
3. 搭好 WorldState / GameMemory / RuntimeState。
4. 搭 Job / JobProposal。
5. 搭最小 Scheduler。
6. 搭 Intent / Executor / Validator。
7. 搭 Outcome 反馈闭环。
8. 写架构 invariant。
9. 写上述基础架构测试。
10. 用几个 DummyJob 验证连续多回合运行。
```

首轮完成后，不要继续自行大规模迁移旧经济/任务/火控逻辑。

先验证：

```text
统一控制权
Job 生命周期
Outcome 反馈
Reservation
Scheduler
```

这些基础架构稳定，再进入下一阶段。

这版我建议就作为 Codex 的“总纲”。后面给 Codex 的具体任务尽量拆成 Phase 1、Phase 2……逐步推进，避免它一次把旧逻辑全迁进去，又重新长成一坨。
