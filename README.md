# 《未来战争》Agent 对战平台（agent-game）

云核心网编程大赛《未来战争》v1.0 的本地开发与对战平台：一套**忠实复刻比赛规则的模拟判题器**、**两个可对战的 agent**（我方智能 agent + 固定策略基线对手）、以及**逐回合可视化回放前端**。全部代码以《接口文档》（`docs/接口文档.md`）与 demo 报文（`docs/request.txt` / `docs/response.txt`）为唯一协议权威实现。

本项目使用 [OpenSpec](https://github.com/Fission-AI/OpenSpec) 进行规格驱动开发：`openspec/` 目录保存了本次变更的完整规划文档（proposal / specs / design / tasks），实现过程严格按规格推进，48/48 任务完成。

---

## 快速开始

```bash
# 1. 依赖（Python 3.9+）
pip3 install --user pyyaml requests

# 2. 配置 LLM 密钥（开发期使用 DeepSeek）
cp deepseek-key.example.md deepseek-key.md   # 然后把 sk- 开头的 key 粘贴进去
# 未配置 key 时系统仍可运行：LLM 返回占位响应，新闻/传闻回退模板文案

# 3. 跑一场完整对战（1300 回合 = 10 个游戏日）
python3 -m sim.judge.runmatch --rounds 1300 --record matches/m1.jsonl

# 4. 前端回放
python3 -m sim.web.server    # 打开 http://127.0.0.1:8080

# 常用变体
python3 -m sim.judge.runmatch --rounds 260 --seed 7            # 短对局 / 换随机种子
python3 -m sim.judge.runmatch --swap-seats                     # 换边对战（我方坐防守位）
python3 -m sim.judge.runmatch --defender-card config/baselines/turtle_defense.json
```

运行测试：

```bash
python3 tests/test_validator.py      # 出站校验器：12 个动作码合法/非法样例
python3 tests/test_engine.py         # 战斗引擎 golden 单测（4.1-4.8）
python3 tests/test_judge_loop.py     # 判题循环集成（1300 回合空转/异常计数）
python3 tests/test_llmproxy.py       # LLM 代理：三上下文隔离/每日配额
python3 tests/test_news.py           # 新闻系统：确定性采样/线索链收敛/塌方三段式
python3 tests/test_sandbox.py        # 沙盒：回填格式/禁网近似/mock API
python3 tests/test_taskagent.py      # 任务代理：流水线/SOP 复用/沙盒持久性
python3 tests/test_competitor.py     # 我方内核：OpponentModel/教义/兜底链/套利/宝藏
```

---

## 系统架构总览

```
                 +--------------------------------------------------+
                 |              判题循环 sim/judge/loop               |
    每回合:      |  组request -> HTTP请求双方agent -> 校验response     |
                 |  -> 沙盒执行executeCmd -> LLM代理转发prompt         |
                 |  -> 引擎结算 -> 对局记录(JSONL)                     |
                 +-----+------------------------+-------------------+
                       |                        |
            +----------v----------+   +---------v-----------+
            |  我方 agent          |   |  对手基线 agent      |
            |  agents/competitor  |   |  agents/baseline    |
            |  +------------------+|   |  +-----------------+|
            |  |OpponentModel     ||   |  |固定策略卡(10天)  ||
            |  |教义×旋钮策略引擎   ||   |  |非任务期零LLM     ||
            |  |每日LLM战略分析    ||   |  |任务期LLM辅助     ||
            |  |任务求解代理(SOP)  ||   |  +-----------------+|
            |  +------------------+|   +---------------------+
            +----------+-----------+
                       | 共用公共骨架 agents/framework/
                       | HTTP server / 回合编排 / 出站严格校验器
                       | LLM异步通道 / 任务求解代理 / 寻路
   +-------------------+-------------------+----------------------+
   |                       战斗引擎 sim/engine/                   |
   | 世界状态 -> 结算(物品>攻击>角色移动>机器人>伤害统一结算)         |
   | -> request视图(视野过滤) -> 积分/胜负                         |
   +-------------------+-------------------+----------------------+
   |    中立新闻系统 sim/news/     |        沙盒 sim/sandbox/       |
   | 参数先行+LLM措辞(独立上下文)   | 子进程执行/15s超时/禁网近似      |
   | 塌方经济影响/宝藏谜题线索链     | mock天气API(确定性判答案)       |
   +------------------------------+-------------------------------+
```

---

## 目录结构

```
agent-game/
├── common/rules.py          # 规则单一权威：动作码/字段表/物品/属性/ID分配/时间公式
├── config/
│   ├── engine.yaml          # 引擎参数：地图/波次/寻路/新闻/宝藏/任务/LLM/沙盒/判题
│   ├── doctrines/           # 我方教义预设卡（balanced/turtle/economy）
│   └── baselines/           # 对手固定策略卡（turtle_defense）
├── sim/                     # 模拟对战平台（扮演比赛判题器+中立系统）
│   ├── engine/              # 战斗引擎
│   ├── judge/               # 判题循环/LLM代理/agent进程编排
│   ├── news/                # 中立新闻系统
│   ├── sandbox/             # 沙盒执行器 + mock任务API
│   └── web/                 # 前端服务 + 回放页面
├── agents/
│   ├── framework/           # 双方agent公共骨架
│   ├── competitor/          # 我方agent
│   └── baseline/            # 对手基线agent
├── tests/                   # 9个测试套件（单测+集成+端到端）
├── docs/                    # 比赛官方材料（只读参照物）
├── openspec/                # OpenSpec规格文档与变更记录
└── matches/                 # 对局记录输出目录（*.jsonl不入库，summary入库）
```

---

## 模块详解

### 1. `common/rules.py` — 规则单一权威

跨 `sim` 与 `agents` 共享的比赛常量与纯函数，禁止任何模块自行硬编码规则口径：

- **动作码全集与字段表**：12 个动作码各自的必填/可选字段（校验器的依据）
- **时间公式**：`day_of / phase_of / is_day_first_round` 等——一天 = 白天 70 + 夜晚 60 回合，1300 回合封顶
- **物品体系**：武器商店全部商品价格、升级券映射、召唤令映射、需要 targetPos 的物品
- **单位属性**：角色/建筑血量表、机器人属性表、角色 ID 分配表（挑战者 10010 系列 / 防守者 20010 系列 / 围墙 40000+ 系列，与《接口文档》1.3.1 一致）
- **几何工具**：切比雪夫距离、相邻判定

### 2. `sim/engine/` — 战斗引擎

纯 Python、零网络依赖的确定性状态机。

| 文件 | 职责 |
|---|---|
| `state.py` | 世界状态：`Unit`（角色/建筑/机器人统一模型）、`Mine`、`TaskPointState`、`TeamState`、`WorldState`。初始化按任务书 4.5.3（金币 75、开拓者×1 工人×2、**武器初始 0 座**需白天建造），矿区随机生成且不落可建造区 |
| `view.py` | `build_request()`：由世界状态生成某一阵营视角的 request 报文。视野规则严格执行——己方全量、敌方仅基地/围墙全局可见+视野距离 4 内单位、机器人全图可见。字段结构与 demo request 做过 golden 对比（`diff_against_demo`） |
| `resolve.py` | 回合结算器，结算顺序固定且文档化：**①眩晕/炸弹物品 → ②角色行动（移动只登记意图）→ ③武器攻击（伤害延迟）→ ④角色移动（三情形碰撞）→ ⑤机器人移动（撞上阻挡者即攻击）→ ⑥伤害回合末统一结算 → ⑦收尾**（冷却/复活/矿区刷新/生存积分/胜负） |
| `provider.py` | 引擎与判题循环的适配层：`build_request / consume / finished` 协议；双方 response 收齐后推进引擎一步；持有新闻系统与 mock 任务 API |
| `config.py` | `config/engine.yaml` 的只读加载视图 |

**关键规则实现**：

- **移动碰撞三情形**（任务书 4.5.4）：①目标点受阻 ②目标点争夺 ③位置互换，均判中断；障碍物相邻的对角线穿行不阻挡
- **武器结算**：加特林每颗子弹沿弹道（Bresenham 栅格线）命中最近机器人 10 伤害、多目标须同一 90° 锥形（向量点积 ≥0 校验）；电磁炮能量 = 10×等级沿路径穿透、按实际伤害扣减；火箭中心 20、8 格溅射 10、落点叠加、发射后 3 回合冷却（发射回合不递减）
- **经济**：每矿采 10 次枯竭、下回合随机刷新；矿石不足平分时每工人各得 1；升级券在满级时"不生效不消耗"；升级后建筑回满血
- **未定义机制全部参数化**（机器人寻路 direct/bfs、波次公式 `(a+b×day)×scale`、出生半径、新闻影响、任务超时/上限）——全部显式声明在 `engine.yaml`，支持多档扫描防止对单一假设过拟合

### 3. `sim/judge/` — 判题循环

| 文件 | 职责 |
|---|---|
| `loop.py` | 回合主循环：组 request → 并行请求双方 agent（连接 10s/响应 5s 死线）→ 协议校验（复用 agent 校验器，指令错误计入异常）→ 沙盒执行（**仅任务期**）→ 逐回合 JSONL 对局记录（双方 request/response 原文 + 引擎事件流） |
| `agentproc.py` | agent 子进程编排：`python -m agents.x.main --port N` 拉起、/health 探活、优雅退出 |
| `llmproxy.py` | DeepSeek HTTP 客户端：密钥从仓库 `deepseek-key.md` 读取（已 gitignore）、指数退避重试、60s 超时、**磁盘缓存**（同 prompt 去重，开发期省额度） |
| `proxy.py` | LLM 代理与配额：agent 的 `prompt` 字段 → 异步转发 → 下回合 `llmResp` 回填；**三上下文隔离**（我方/对手/新闻各持独立历史，互不可见）；**每日 3 次配额**（游戏日界重置；超限不转发并向 errors 写 errorCode=5；任务期豁免不计数） |
| `runmatch.py` | 一场对战的编排入口（支持 `--swap-seats` 换边、`--defender-card` 换基线卡） |
| `demo_provider.py` | 测试用 demo 报文状态源 |

**异常口径**（与任务书第八章严格一致）：响应超时/格式错误/指令错误计 1 次异常，累计 5 次停调度；**指令执行失败**（碰撞、落点无目标等）只标记 `lastRoundRoleActionResults=false`，不计异常。

### 4. `sim/news/` — 中立新闻系统

设计原则：**参数先行、LLM 措辞**。事件参数（矿种/影响天数/价格倍率/停采）与宝藏谜题答案在开局用固定种子确定性采样并锁定，LLM 只负责把参数写成叙事文本（改写时硬性要求保留事实数字），不可用时回退模板。

- **官方消息**：塌方（停采 2 天 + 价格 ×2）/发现（跌价）/紧急订单（涨价）三类事件，"公告当天不受影响、次日开始、N 天后恢复"的三段式结算
- **民间传闻**：宝藏谜题（地点/祭品组合/开启窗口三元组）分 5 天释放线索，读齐后可唯一推断答案；`summonTreasure` 结果码 1-4 语义完整实现（**祭品错误也会消耗**——高信心才出手）

### 5. `sim/sandbox/` — 沙盒

- `exec.py`：每队独立子进程沙盒，家目录 `sandbox_runtime/<team>/` **跨任务/跨回合持久**（SOP 沉淀的基础）。回填格式严格按接口文档：`[exitCode:N]\n<输出>`、`[TIMEOUT]`、超 64KB 追加 `[TRUNCATED]`。禁网近似：http(s)_proxy 指向死端口（localhost 不受影响，mock API 可达）
- `mockapi.py`：确定性天气查询 mock API（城市→天气固定映射表），引擎判答案与 API 返回共用同一张表

### 6. `sim/web/` — 前端可视化

Python 标准库 HTTP 服务 + 单文件 HTML/Canvas/原生 JS（零构建链）：

- 41×32 战场网格渲染，单位按阵营/类型着色并带血条，机器人标注类型
- 逐回合步进/播放/调速/回退，动作目标位置叠加金色标记
- **视角切换**（挑战者/防守者）——严格按该方 request 的实际可见性渲染
- 资源面板（金币/积分/武器/围墙）、积分随回合曲线、按类型过滤的事件日志（点击跳转回合）

---

### 7. `agents/framework/` — 公共骨架

双方 agent 共用的运行时，**协议合规由它统一保证**：

| 文件 | 职责 |
|---|---|
| `server.py` | HTTP server（`bash run.sh port` 形态），POST / 收 request 返 response，内部异常兜底空响应，进程永不因单回合错误崩溃 |
| `validator.py` | **出站严格校验器（生命线）**。三层：① schema——动作码合法、必填字段齐备（move 缺 targetPos、use 眩晕法宝缺 targetPos 等即"指令错误"）；② 语义——坐标域、ID 归属、昼夜门（白天禁 attack/build）、角色类型门（工人才能 build/collect）、多目标数=武器等级、加特林锥形 ≤90°；③ 兜底——非法指令剔除不出站。**纯确定性、零 LLM**，并处理"操控武器的角色不能另有指令"的归属规则 |
| `orchestrator.py` | 回合编排：策略计算带时间预算（超时/异常回退"上回合合法指令"重放），与判题器 5s 死线解耦 |
| `llmchannel.py` | LLM 异步通道：agent 只填 `prompt` 字段（**绝不直连 LLM API**——比赛期 LLM 由判题器提供），下回合读 `llmResp`；每日 3 次配额本地自律（任务期豁免） |
| `taskagent.py` | 任务求解代理（见下文"自进化"） |
| `pathing.py` | 基于视野的 BFS/贪心寻路 |

### 8. `agents/competitor/` — 我方 agent

**设计分工：代码执行策略，LLM 分析与调参。**

- **`opponent_model.py` — OpponentModel**：每回合从全局信息通道 diff 收集对手确定性动作——敌方基地/围墙的增删/升级/受损（全图可见）、机器人血量掉落（火力指纹：每跳伤害直方图推断武器构成与 DPS）、骚扰检测（夜间第 1 回合后新出现的机器人=召唤令证据）、视野内敌开拓者贴任务点（任务活动推断）。输出动作事件流 + 滚动特征序列（围墙增速等），全部纯函数、可用对局记录离线回放验证
- **`doctrine.py` — 教义×旋钮**：策略 = 离散教义（4~6 张预设卡）× 连续旋钮（骚扰预算/围墙密度/任务节奏/采集偏好/升级优先级/囤卖时机等）。每个旋钮有**硬取值域夹紧**——LLM 幻觉输出最坏情况是被夹紧，不可能自杀；`apply_overrides` 白名单过滤未知字段
- **`engine.py` — 确定性调度**：日常调度零 LLM。新闻推理（官方消息关键词→囤卖套利：涨价事件在手立即抛售持有矿种、跌价矿种降权采集）；宝藏求解（传闻线索正则收敛出地点/祭品/窗口三要素→齐备后高信心召唤，只试一次）；夜间火控（每武器分配最近操控角色打射程内最低血）
- **`agent.py` — 编排与每日 LLM 分析**：日界第 1 回合发合并 prompt（一屏仪表盘 + 教义索引 + 今日新闻，输入恒定约 1k token，10 天对局也不增长），期望输出一小段 JSON（选教义+微调旋钮）；`llmResp` 下回合到达 → 正则提取 JSON → schema 校验 → 夹紧 → **原子热替换**当日参数。兜底链：LLM 输出 → 昨日教义 → 全局默认，任何失效都不空转

### 9. `agents/baseline/` — 对手基线

固定策略卡（`config/baselines/`）驱动，10 天一条路走到黑：白天采集→贩卖→按卡建武器/围墙，夜晚全员操炮；**非任务期 prompt 恒为空**；行为确定性可复现（相同种子重放指令序列逐回合一致）；任务期与公共 TaskAgent 一致，保证任务线公平基线。

### 10. 任务求解代理与自进化（`taskagent.py`）

自进化类任务（沙盒任务，如"调用天气 API 查询城市天气"）的通用求解循环：

```
首次探索（LLM+沙盒双通道并行，2回合）:
  R1: prompt 请教 LLM 怎么调 API ＋ executeCmd 直接 curl 试探（不等LLM）
  R2: 读 lastCmdResult 提取天气 -> submitAnswer
      ＋ 同时 executeCmd 把可复用脚本 base64 落盘到沙盒（SOP沉淀）
SOP 复现（同类任务再现，2回合零LLM）:
  R1: executeCmd 直接运行已沉淀的 sop_weather.sh
  R2: 读结果 -> submitAnswer（速度加成 5×超时回合/实际回合 拉满）
```

沙盒文件系统跨任务/跨进程持久（已实测），SOP 一旦沉淀永久可用；agent 侧"已学会"记忆为进程内，跨进程重启时可用一个探针回合检测脚本存在性。

---

## 关键架构设计

### 为什么 agent 不直接调 LLM？

比赛时 LLM API 由判题系统提供（response 里填 `prompt` 字段，结果下回合从 `llmResp` 取回，1 回合延迟）。本平台让**模拟判题器扮演比赛判题器**去调 deepseek——agent 代码在开发期与比赛期**零改动**，只换判题器的 LLM 后端。

### 为什么出站校验器是生命线？

赛制：异常响应（超时/格式错误/指令错误）累计 5 次即淘汰。校验器保证任何策略层产物——包括 LLM 生成的、内部 bug 产生的——都不可能以非法报文出站；同时校验器做"静态可判定"的规则，依赖实时状态的合法性（如目标点是否被占）交给引擎按"指令执行失败"处理（不计异常）。

### 为什么策略要拆成教义×旋钮？

枚举 N 套完整策略人力不可持续，且上下文随库膨胀。参数化后：新策略 = 改几个数；LLM 只需输出一小段 JSON（选标签+微调几个数），上下文恒定一屏；旋钮夹紧保证 LLM 幻觉无害；策略库可由模拟平台的参数扫描自动"长"出来（后续变更）。

### 为什么新闻要"参数先行"？

若让 LLM 自由生成新闻，其经济影响不可计算。先确定性采样影响参数（引擎按参数真实结算），再让 LLM 把参数"写"成叙事——文本只需可推断参数，地面真值可控、可测试。

---

## 与比赛接口的对齐

- 报文字段与《接口文档》1.1/2.1 顶层结构**字段级对齐**（demo request golden 对比通过）
- 角色可见性：敌方仅基地/围墙全局可见，其余进视野（距离 4）才可见；机器人全图可见
- 每游戏日 3 次 LLM 配额、日界重置、任务期豁免不计数、超限 errorCode=5
- `executeCmd` 仅任务期可用、15s 超时、`[exitCode:N]`/`[TIMEOUT]`/`[TRUNCATED]` 回填格式
- 已知文档矛盾（demo 报文攻击距离 vs 任务书表格、demo JSON 书写瑕疵等）的取舍记录在 `openspec/changes/sim-platform-and-agents/design.md` 的 Open Questions 一节

## 测试与验收

- 9 个测试套件全绿（校验器/编排/判题循环/引擎 golden/新闻/沙盒/LLM 代理/任务代理/我方内核）
- 端到端：两场完整对战（476 回合与 607 回合，含换边）**双方 0 异常**、零非法报文
- 每日 LLM 分析 4 发 4 中、2 回合周期回填；SOP 复用任务 2 回合零 LLM 完成
- `openspec/` 保存完整规格与设计决策（含 9 条实测回填的 Open Questions）

## 后续计划

1. **参数扫描与自进化养库**：基线动物园 × 旋钮网格扫描 → 聚类胜出区域自动生成教义卡 → 教练 LLM 读战报提炼新卡（开发期 deepseek 无限调用）
2. **对手画像驱动的 k-NN 推荐**：对手特征向量 → 推荐参数向量查找表，LLM 降级为审计+纠偏
3. 防守强度优化（当前换边对战中我方防守位偏弱，正是平台暴露出的第一改进点）
4. 比赛环境对接：`config/engine.yaml` 的 llm 段换成平台 API；`attack` 挂载键等 2 项协议细节按真实判题器复核
