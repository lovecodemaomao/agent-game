# sim-judge-loop 判题循环规格

## Purpose

以《接口文档》规定的 HTTP 报文协议驱动双方 agent 进行逐回合对战，充当 LLM 代理与沙盒执行器，执行每日 LLM 配额与异常计数，判定比赛结束与胜负，是本地模拟的调度中枢。

## ADDED Requirements

### Requirement: 报文协议驱动
判题循环 SHALL 通过 HTTP 向双方 agent 监听端口发送 request 报文并收取 response 报文，字段结构与《接口文档》及 demo request/response 完全一致（roundNo、mapInfo、teamOur、teamEnemy、robot、phaseTask、lastRoundRoleActionResults、lastSummonTreasureResult、llmResp、worldNews、lastCmdResult、vendorShopList、weaponShopList、errors）。request 中的队伍可见性 MUST 遵循视野规则：己方全量信息、敌方仅基地/围墙全局可见+视野内单位、机器人全图可见。request 里各字段的动作结果回填 MUST 反映上一回合的真实结算。

#### Scenario: 报文字段完整性
- **WHEN** 判题循环向 agent 发送任一回合的 request
- **THEN** 报文包含《接口文档》1.1 节顶层结构的全部字段且类型正确

#### Scenario: 视野过滤
- **WHEN** 敌方角色位于己方所有单位视野（距离 4）之外
- **THEN** 该角色不出现在我方 request 的 teamEnemy.roles 中

### Requirement: LLM 代理与每日配额
判题循环 SHALL 承接 agent response 中的 `prompt` 字段：调用平台侧 LLM（deepseek-v4.1-flash）并将结果在下一回合的 request `llmResp` 字段回填。每队每个游戏日（130 回合）MUST 限制 3 次非任务期调用，在每个游戏日第一回合重置；超出配额 MUST 在 errors 中返回 errorCode=5（LLM 额度超限）。自进化任务期间（接取到任务结束）调用不受限制且不计入当日配额。三个 LLM 调用上下文（我方 agent、中立新闻系统、对手 agent）MUST 互相独立、互不共享历史。

#### Scenario: 配额超限
- **WHEN** 我方 agent 当日已发送 3 次 prompt 后再次发送
- **THEN** 该 prompt 不被转发给 LLM，errors 中出现 errorCode=5

#### Scenario: 任务期豁免
- **WHEN** 我方开拓者处于自进化任务执行期间连续发送 prompt
- **THEN** 每次调用均被转发给 LLM 并在下回合回填，且不计入当日 3 次配额

#### Scenario: 上下文隔离
- **WHEN** 我方 agent 的某次 prompt 中询问"对手 agent 上一轮说了什么"
- **THEN** 我方上下文中不存在对手 agent 的任何对话历史，LLM 无法回答该信息

### Requirement: 沙盒执行
判题循环 SHALL 为每队提供独立沙盒执行 `executeCmd` 命令（shell 与 python），单次执行时长不超过 15 秒，结果以 `[exitCode:N]\n<输出>` 格式在下一回合 request 的 `lastCmdResult` 回填；超时回填 `[TIMEOUT]`、输出超 64KB 追加 `[TRUNCATED]`。沙盒 MUST 无法访问外部网络，且仅任务期间允许使用该字段。沙盒内 MUST 提供 mock 的第三方任务 API（如天气查询）供自进化任务探索。

#### Scenario: 命令结果回填
- **WHEN** agent 提交 executeCmd 为 `echo hello`
- **THEN** 下一回合 request 的 lastCmdResult 为 `[exitCode:0]\nhello`

#### Scenario: 超时保护
- **WHEN** agent 提交 `sleep 30` 作为 executeCmd
- **THEN** 执行在 15 秒被终止，lastCmdResult 回填 `[TIMEOUT]` 标记，且不计入队伍异常

#### Scenario: 外网隔离
- **WHEN** agent 在沙盒内执行 `curl http://external.example.com`
- **THEN** 命令因无法访问外部网络而失败

### Requirement: 异常计数与淘汰
判题循环 SHALL 对以下三类情况计 1 次队伍异常：连接建立超 10 秒、发送请求后 5 秒内无响应、响应格式错误（不符合《接口文档》规范）、指令错误（动作码非法或必填字段缺失，如 move 缺 targetPos、use 眩晕法宝/范围炸弹未指定 targetPos）。单队累计 5 次异常后 MUST 停止调度该队伍直至比赛结束。指令合法但因规则未生效（碰撞、落点无目标等）MUST NOT 计为异常，仅在 lastRoundRoleActionResults 标记为 false。

#### Scenario: 响应超时计异常
- **WHEN** agent 超过 5 秒未返回 response
- **THEN** 计 1 次异常，该回合按空指令处理

#### Scenario: 指令失败不计异常
- **WHEN** agent 发出合法 move 指令但因碰撞未生效
- **THEN** lastRoundRoleActionResults 中该角色为 false，异常计数不变

### Requirement: 比赛编排与胜负输出
判题循环 SHALL 支持配置对阵双方（agent 地址/策略卡/阵营分配），完整跑完一场（最多 1300 回合或提前结束条件），输出逐回合完整对局记录（每回合双方 request/response 原文、结算结果、动作有效性）与最终积分/胜负结论。上半场 challenger/defender 分配 MUST 可配置以便复现换边对战。

#### Scenario: 完整对局输出
- **WHEN** 一场比赛结束
- **THEN** 生成包含全部回合数据的对局记录文件，可用于前端回放

#### Scenario: 双方异常终止
- **WHEN** 某队异常响应累计达到 5 次
- **THEN** 该队不再被调度，比赛按规则处理其结果
