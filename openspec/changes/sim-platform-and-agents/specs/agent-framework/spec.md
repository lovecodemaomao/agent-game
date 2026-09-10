# agent-framework agent 公共骨架规格

## Purpose

提供对战双方 agent 共用的运行骨架：HTTP 接入、回合编排、LLM 异步通道管理、任务求解代理，以及最关键的出站严格格式校验器——保证任何输出到模拟平台的报文都符合《接口文档》规范。

## ADDED Requirements

### Requirement: HTTP server 接入
agent SHALL 以 `bash run.sh port` 方式启动并监听指定端口的 HTTP 服务，在判题循环请求到达时返回符合 Response 2.1 顶层结构（roleCommandMap、prompt、executeCmd）的报文。每个角色每回合至多 1 条指令，角色 ID 与指令 MUST 匹配合法归属。

#### Scenario: 标准启动
- **WHEN** 以端口 18080 启动 agent 并收到判题循环的回合请求
- **THEN** 在时限内返回含 roleCommandMap/prompt/executeCmd 三字段的合法 response

### Requirement: 出站严格格式校验器
agent SHALL 在提交任何 response 前经过一层确定性校验器：校验 action 取值在动作码全集内；校验必填字段存在且类型正确（move/build/remove/collect/attack 等必须有 targetPos；sell/buy/use/drop 必须有 name；attack 必须有 controllerId；summonTreasure 必须有 targetPos 与 item）；校验坐标在 41×32 地图内；校验角色 ID 归属与动作可用性（如白天禁止 attack/build、非工人禁止 build/collect、非开拓者禁止 acceptTask）；校验加特林/火箭多目标数量与武器等级一致、加特林锥形 ≤90°。校验 MUST 为纯确定性规则（不依赖 LLM）。任何不合规指令 MUST 在本地被剔除或修正，禁止原样出站。

#### Scenario: 剔除非法指令
- **WHEN** 策略层生成了一条白天 attack 指令
- **THEN** 校验器剔除该指令，该角色本回合不提交动作，出站报文不含它

#### Scenario: 字段缺失兜底
- **WHEN** 生成的 move 指令缺失 targetPos
- **THEN** 校验器拦截该指令，出站报文不含该指令，且不出现代码异常

#### Scenario: 零非法出站
- **WHEN** 对 agent 在整场比赛中提交的全部 response 报文做协议检查
- **THEN** 不存在任何"指令错误"级别的非法报文（errorCode=4 类）

### Requirement: 回合编排与超时防护
agent SHALL 在收到请求后的固定时限内返回响应；内部计算异常或超时 MUST 兜底为合法的最小响应（如全员保持上回合合法指令或空 roleCommandMap）。agent 进程 MUST 不因单回合内部错误崩溃退出。

#### Scenario: 内部异常兜底
- **WHEN** 某回合策略计算抛出未捕获异常
- **THEN** 仍按时返回合法 response（如空指令），进程存活，异常被记录供复盘

### Requirement: LLM 异步通道管理
agent SHALL 通过 response 的 `prompt` 字段发起 LLM 调用、在下一回合 request 的 `llmResp` 读取结果，维护未完成调用表；并按每游戏日 3 次的预算管理非任务期调用，任务期内无限制。agent MUST NOT 自行直接调用外部 LLM API——所有 LLM 交互均经判题循环转发。

#### Scenario: 一回合延迟回填
- **WHEN** agent 在第 40 回合发送 prompt
- **THEN** 第 41 回合 request 的 llmResp 携带该次调用的回答

#### Scenario: 每日配额自律
- **WHEN** 当日已用 3 次非任务期 prompt
- **THEN** agent 不再发起非任务期 prompt，直至次日第一回合

### Requirement: 任务求解代理
agent SHALL 提供自进化任务求解流程：解析 `phaseTask` 任务描述 → 通过 `executeCmd` 在沙盒中探索（调用 mock API、运行脚本）→ 沉淀可复用 SOP 脚本 → 匹配任务类型快速执行 → `submitAnswer` 提交。任务期内 MAY 高频使用 LLM 与沙盒流水线并行推进。

#### Scenario: 首次任务探索
- **WHEN** 开拓者领取"通过 API 查询北京天气"类任务
- **THEN** agent 通过 executeCmd 调用 mock API 获取结果并提交答案

#### Scenario: SOP 复用加速
- **WHEN** 同类型任务（查询上海天气）再次出现
- **THEN** agent 直接运行已沉淀的 SOP 脚本完成，显著缩短完成回合数
