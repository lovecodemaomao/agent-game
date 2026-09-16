# CoreGeek V2 Demo

以策略提交 `780e98e42e0053d9828b9658e820c6a042cf82a6` 为基线完善的独立 Demo。生产入口已接入采矿、施工、采购、任务、宝藏和整夜防守；Python 3.11+，运行时仅使用标准库，不导入旧版 `agent`。

规则依据为仓库根目录 `docs/任务书.md`、`docs/接口文档.md`。一天 70 回合白天、60 回合夜晚，最多 1300 回合。

## 启动与接入

在仓库根目录：

```bash
python -B Demo/CoreGeekV2/main3.py 8081
# Linux 比赛入口
bash Demo/CoreGeekV2/run.sh 8081
```

服务监听 `0.0.0.0:<port>`，向任意路径 POST 官方 JSON Request，响应固定包含：

```json
{"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
```

`roleCommandMap` 由当回合策略填充。攻击以武器 ID 为键，`controllerId` 为操作者 ID；每个角色每回合最多一条命令。`prompt` 和 `executeCmd` 交给平台处理，下一回合通过 `llmResp`、`lastCmdResult` 回传。**无需配置真实 LLM、密钥或本地任务沙盒**；Demo 不自行执行输出的命令，不发送外部 API 请求。

默认比赛模式，错误写入标准错误日志；异常请求不覆盖已提交状态。开发时用 `Agent.production(development=True)` 暴露断言。按队伍隔离、同回合缓存、回合回退重置。会话只在内存中，新比赛应重启进程；同队伍同回合的新局无法与重试区分。

## 已接入策略

| 模块 | 行为 |
| --- | --- |
| 每日目标 | Day1 两 Rocket、一 Railgun、正面四墙；Day2 半圈十墙、优先武器 Lv2；Day3 基地券、武器 Lv3、关键墙；后续按生存和火力维护 |
| Worker A | 铜铁经济、Day1 辅助施工；动态比较矿点收益和完整收尾耗时 |
| Worker B | 独立石料配额：待建墙、普通墙重建和一块备用石；满足后转经济，负责施工 |
| Pioneer | 可行任务 → 已有券交付 → 采购 → 商店待命；回防截止与紧急维护优先于普通业务 |
| 券与维护 | 武器券及时使用；基地／关键墙券结合损伤和威胁保留回血价值；普通 Lv1 墙拆建，关键墙升级，高等级墙维护 |
| 回防 | 返程成本包含计划建成的墙、其他岗位占用；三岗位互不封路；施工前检查占用与建成后通路 |
| 夜间 | 固定双 Rocket 操作员、Railgun 操作员和辅助员；持续检查冷却、火控、维修和已持有消耗品 |
| 任务 | 探测 → 平台命令／LLM 回填 → 答案提交；关联请求与任务，保留成功方法供同题重用 |
| 新闻／宝藏 | 仅接受有原文证据的建议；普通新闻每日最多三次提示；宝藏使用官方 0–4 结果码 |

这是可接入测试的策略 Demo；日期目标取决于收入、地图、角色存活和平台任务反馈，并非对任意对局收益或存活十天的保证。

## 打包

```bash
python -B Demo/CoreGeekV2/package_demo.py --verify
```

输出 `Demo/CoreGeekV2/dist/CoreGeek.tar.gz` 和同名 `.sha256`。压缩包必须保留顶层 `CoreGeek/` 目录，里面是 `run.sh`、`main3.py` 和 `src/`，对应平台 `/home/docker/CoreGeek/main3.py`；包含官方规则副本、架构说明和哈希清单，不包含缓存、Git 历史或模拟器依赖。

```bash
mkdir coregeek-v2
tar -xzf CoreGeek.tar.gz -C coregeek-v2
cd coregeek-v2/CoreGeek
bash run.sh 8081
```

打包脚本在临时目录解压、逐文件验哈希，用隔离 Python 子进程启动 HTTP 服务，验证官方请求、重复请求和下一回合响应。

## 验证与文档

V1 / V2 测试在独立进程运行：

```bash
python -B -m unittest discover -s Demo/CoreGeekV2/tests -v
python -B -m unittest discover -s Demo/CoreGeek/tests -v
```

可选模拟器诊断（需安装仓库模拟器依赖，不使用真实 LLM）：

```bash
python -B Demo/CoreGeekV2/tests/smoke_match.py --rounds 1300 --seed 42 --output smoke.json
```

模拟器只用于发现长局问题，规则以正式文档为准。任务命令／答案闭环由受控官方 Payload 测试覆盖；平台真实任务成功率需接入后验证。

- [架构及所有权](ARCHITECTURE.md)
- [验证记录与限制](VALIDATION.md)
- 源码目录的 `docs/REFACTOR_BRIEF.md`、`docs/DAY_NIGHT_STRATEGY.md` 保存原始需求。
