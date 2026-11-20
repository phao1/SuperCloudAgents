# Mac 执行记录评审（负责人复核）

评审对象：[portal-run-2026-11-19.md](portal-run-2026-11-19.md)、[逐事件记录](records/2026-11-19-order-events.jsonl)，以及提交 `a05be72`、`b0e8e72`。
评审日期：2026-11-20。评审人：负责人（在 Linux 服务器上复核代码与事件，未在 Mac 上复跑）。

## 1 总体结论

- 记录**基本可信**。运行 ID、事件序号、时间线和代码行为互相对得上。脚本化复验（`508405e9`）已经明确标注“不计入真实模型结果”，这一点做得对。
- 记录暴露了 **1 个设计缺陷**，并且有 **1 处失败根因写得不准**：
  - 设计缺陷：记忆和规则读取得太晚，见 P1。
  - 根因不准：首次试跑失败的真正原因是一次 Web Search 权限询问，见 P2。
  - 两处都已修复。记忆时机的修复在本服务器上用真实模型做了现场验证，见第 5 节。
- 负责人复核时另发现一处问题：**评测任务默认读写共享记忆**。这会污染记忆审核队列，也让评测结果不可复现。见 P12，已修复。
- 覆盖面：订单任务点亮了原图 50 个节点中的 33 个，但其中 3 个节点属于**映射虚高**（P4）。
  - 架构图共 64 个模块。这次 Mac 运行在真实平台上实际跑到的约 30 个。
  - 剩下的模块分三类：一是需要打开开关或跑另一条闭环才能覆盖，二是目前只能在 Linux 宿主机上跑，三是还没实现。见第 3 节。

## 2 逐条校验

| # | 记录里的说法 / 现象 | 校验结果 | 处理 |
|---|---|---|---|
| P1 | 共享记忆在 `#133`（合并之后）才读取，注入 0 条；复验 `508405e9` 的 `#61` 命中“整数分”记忆 | **设计缺陷，说明见下方** | Codex Y 修复：任务开始时读一次，规划和每个子任务都注入，子任务的记忆提案也收集。用 `workflow.patched` 保证在途任务不受影响 |
| P2 | 首次试跑 `de960065` 失败，记录写的是“Agent 未返回完整执行轮” | **根因不准，说明见下方** | 已修：`src/sca/agents/opencode.py`、`deploy/opencode/brain/host.ts` 在配置里对 `websearch`、`webfetch` 直接 `deny`，模型拿到拒绝后会继续。真实 OpenCode 二进制 `debug config` 已确认能解析该配置 |
| P3 | Ruff `command not found`，改为 `python -m ruff` | 合理。修复点正确（`src/sca/intents/rules.py`） | — |
| P4 | 回放点亮 33/50 节点 | **有虚高，说明见下方** | Codex Z 修正 `task-flow.js`；另做 `scripts/coverage_report.py`，按事件客观统计 64 个模块的覆盖 |
| P5 | 记忆“按租户、任务类型和仓库范围查询” | 查询路径正确。但只验证了**读**：记忆库是空的，“提案 → 审核采纳 → 下个任务命中”的**写**路径没跑。Portal 的审核面板也不会发送 `accept_memories`，点页面根本做不到 | Codex V 在 Portal 加记忆审核和“共享记忆”视图 |
| P6 | 提示词基线 4/6，“真实评测运行停在基线” | 属实。自演进闭环没走完：`evo_improve`、`evo_release`、`opencode_versions`、`portal_loop` 在 Mac 上都没有覆盖 | 测试计划 S2、S3 |
| P7 | 订单任务费用 0.012738 美元（任务事件用量口径） | 只是主工作流的费用，不含规划和三个子任务。报告总成本时应写合计，或注明口径 | 测试计划要求按 `coverage_report` 和看板给出合计 |
| P8 | “临时容器曾补入口定位问题” | **不合规**：手工修改运行中的沙箱会污染证据 | 以后失败要原样留档，修代码后重新提交任务。已写进测试计划规则 |
| P9 | 三个子任务都由同一个 Worker 进程处理 | 只证明了 Temporal 的并行子工作流，没有证明多 Worker 分布 | 测试计划 S12（可选：起第二个 Worker） |
| P12 | （负责人在服务器上发现）待审记忆约 100 条，几乎全部来自评测任务，内容大多是“运行 python verify.py”之类的重复提案 | **问题**：评测任务没有设置 `memory_enabled`，所以默认开启，每次评测都会读写共享记忆 | 已修：`src/sca/eval/activities.py` 让评测任务默认关闭记忆，套件或评测参数可以显式打开。测试 `test_eval_submission.py` 4 passed |
| P10 | macOS 单元测试 428 passed、1 skipped、2 deselected | 合理。Linux 上是 451 passed；差额是 Linux 专用的 brain 测试文件，以及两条 overlay 属性用例 | — |
| P11 | 回放说明卡、全屏、质量看板 | 代码在 `b0e8e72`。负责人在 Linux 上跑单元测试 451 passed，ruff 全部通过 | — |

**P1：为什么记忆读得太晚**

`TaskWorkflow._run` 先执行 `coding.before_turn` 扩展，规划、拆分审核、三个子任务、合并都在这一步完成；之后才执行 `compile_intents` 和 `memory_initial_prompt`。这带来三个后果：

- 规划 Agent 和子任务 Agent **看不到记忆，也看不到规则说明**。复验里那条“整数分”记忆，是在 `totals` 子任务写完代码之后才被读到的。
- 有拆分时主任务第 1 轮直接跳过（`skip_turn`），所以记忆只有在 CI 失败触发修正轮时才会真正被 Agent 读到。
- 子任务写的记忆提案会直接丢失。

用户问“为什么不是项目开始就读取”，这个问题问到了点子上。

**P2：首次试跑失败的真正原因**

真实根因在事件 `#230`：`Cancelled a request for input this run cannot answer: Web Search`。

Agent 想联网搜索 ruff 的问题，OpenCode 因此弹出权限询问。无头运行没人能回答，平台只能取消这次询问，OpenCode 随即把整轮判为失败（见 `.data/opencode/src/packages/cli/src/run/noninteractive.ts` 的 `cancelForm`）。之后两次重试也都以同样方式失败。

**P4：哪些节点是虚高**

- `intents.compiled` 会无条件点亮 `design_in`、`design_spec`。这次任务没有任何设计稿输入，点亮这两个节点没有依据。
- 普通任务的 `acceptance.completed` 也会点亮 `evo_eval`。但 `evo_eval` 是自演进的评测沙箱，不是普通任务的独立验收。

## 3 架构图覆盖分析（64 模块）

用 `python3 scripts/coverage_report.py --jsonl Milestone/records/2026-11-19-order-events.jsonl` 做客观统计：

- 有事件可以证明的：25 / 64。
- 只能人工核对的：23 个，包括 UI 操作、观测后端、缓存命中等。
- 未实现：1 个。
- 未覆盖：其余 15 个。

离线记录做过脱敏，审核人、CPU/内存等字段被去掉了，所以在线模式（`--since`）的统计会略高一些。

本节先是手工核对的版本。Codex Z 交付后，以 `python3 scripts/coverage_report.py` 的输出为准（见 [coverage-test-plan.md](coverage-test-plan.md)）。

### 3.1 Mac 运行已真实覆盖（约 30 个）

| 层 | 模块 |
|---|---|
| 入口 | `ui_goal`、`ui_dispatch`、`ui_quality`、`ui_confirm`、`ui_approve`（规划审核）、`ui_history`（回放） |
| 编排 | `task_entry`、`planner`、`decouple`、`dag`、`subtask_q`、`checkpoint`、`optim_entry`（CI 反馈修正） |
| 运行时 | `sandbox_sched`、`sandbox_pool`、`runtime`、`executor`、`recover_gate`（暂停释放后在新容器恢复） |
| 共享与底座 | `identity`（按轮签发 key）、`audit`、`object_store`、`repos`、`compute`、`obs_store`、`obs_cost` |
| 验证 | `design_intent`（规则意图编译）、`verify_gw`、`verify_parser`、`quality_echo` |
| V2 | `model_gateway`、`admission_control`。这两个有事件 `model.key_issued`、`admission.granted`，但原图回放没有对应节点；由覆盖报告补算 |
| 评测 | `evo_goal`、`evo_eval`、`trajectory_checks`、`opencode_versions`（仅基线 `9ddc22f5`，用的是已有的 sca.1 版本） |

`memory` 只覆盖了读路径，而且命中 0 条（真实运行）；命中 1 条的那次是脚本化复验。

### 3.2 未覆盖：原因与覆盖方法

| 模块 | 为什么没覆盖 | 怎么覆盖 | 需要开发吗 |
|---|---|---|---|
| `memory`（写路径）、`vector_store` | 记忆库为空；Portal 没有审核和采纳入口；没配 embeddings 模型 | S1：建记忆或采纳提案 → 第二个任务命中。`vector_store` 需要真实的 embeddings 模型，没有就只验证关键词召回 | **要**：Codex Y（读取时机）、Codex V（Portal 入口） |
| `design_in`、`design_spec` | 没有设计稿或 Token 输入（Figma 接入未实现） | 目前只能覆盖“规则文件作为规范输入”。真正的设计稿接入属于规划项 | 后续：工具域 / MCP |
| `evo_trigger`、`evo_improve`、`evo_release`、`portal_loop`、`obs_risk` | 闭环停在基线 | S2（提示词闭环）、S3（opencode-demo 闭环） | 不需要 |
| `pi_backend` | 没换后端 | S4 | 不需要 |
| `gateway_limiter` | 默认关闭 | S5：`SCA_GATEWAY_LIMITER=true` | 不需要 |
| `workspace_forks`、`workspace_overlay` | 默认关闭 | S6：`SCA_WORKSPACE_SNAPSHOTS=true`。overlay 在 Docker Desktop 的 Linux VM 里可以尝试，失败就记为平台限制 | 不需要 |
| `handd`、`idle_suspend` | 默认关闭 | S7 | 不需要 |
| `net_sec` | 没有主动测试出站拦截 | S8：让 Agent 访问白名单外的域名，必须被拒 | 不需要 |
| `cache_dep`、`cache_code`、`cache_gw` | 回放映射里没有对应事件 | S9：同一仓库连续两次 setup，对比耗时；镜像层复用 | 不需要（由覆盖报告用 `setup.completed` 计时判断） |
| `analysis_kind` | Portal 没有入口，setup 也不构建分析镜像 | S10 | **要**：Codex V |
| `obs_trace`、`obs_replay` | 没有启动 obs profile 和 OTLP | S11：`--profile obs` + `SCA_OTLP_ENDPOINT` | 不需要 |
| `kms` | 没启用 OpenBao | S11：`--profile openbao` + `SCA_SECRET_PROVIDER=openbao` | 不需要 |
| `k8s` | 需要 kind | S12：kind 两节点。Docker Desktop 需要 ≥ 8 GB 内存，可选 | 不需要 |
| `hand_brain`、`brain_split`、`brain_pool` | 脑手分离在宿主机上运行 Linux 版 bun，只支持 Linux 宿主机 | 已在 Linux 服务器上现场验证。Mac 上不覆盖，记为平台限制 | 若要在 Mac 上覆盖：brain 放进容器运行（另立项） |
| `knowledge`（企业文档）、`tool_domain` | 未实现（`planned` / `partial`） | 无法覆盖。按选型评审接 MCP 网关、Onyx、Zoekt | **要**：另立项（1–2 周） |

## 4 本次修复与开发清单

| 编号 | 内容 | 状态 |
|---|---|---|
| 负责人 | OpenCode 无头运行时拒绝 Web 工具（P2） | 完成。单元测试 41 passed；用真实 OpenCode 二进制做 `debug config`，解析结果为 `websearch: deny` |
| 负责人 | 评测任务默认关闭共享记忆（P12） | 完成。`test_eval_submission.py` 4 passed |
| Codex Y | 任务开始时先读记忆、编译规则意图（`workflow.patched("memory-before-planning")`）；注入规划 Agent 和每个子任务；收集子任务的记忆提案；检查点提交排除 `.sca/` | 完成。说明见下方 |
| Codex Z | `Milestone/coverage-map.json`（64 个模块的证据规则）、`scripts/coverage_report.py`（在线和离线）；修正回放的两处虚高（P4） | 完成。说明见下方 |
| Codex V | Portal 审核时可以勾选并采纳记忆提案；新增“共享记忆”视图（新建、停用、删除）和“数据分析”入口（上传 CSV/JSON、提问、查看报告）；setup 构建 `sca-sandbox-analysis` 镜像 | 完成，静态测试 6 passed。**浏览器交互未实测**：服务器上没有可用的浏览器自动化，由 Mac 上的 S1、S10 首次实测 |

**Codex Y 的负责人复核**

- 负责人发现并修掉一个回归：检查点提交用了 `':(exclude).sca'`，而 `.sca` 已在忽略列表里，git 遇到这种写法会直接报错，导致拆分任务的 e2e 失败。现在改成只执行 `rm --cached`，e2e 复跑通过。
- 3 个旧单元测试改为适配 `workflow.patched`。

**Codex Z 的负责人复核**

- 负责人修正了一条规则：真实的审核事件里 `plan` 字段是一个对象，原规则按 `plan == true` 精确匹配，所以在线模式永远不会命中。
- 补了两条规则：CI 或验证失败后的修正轮（对应 `optim_entry`）；离线记录里记忆事件的 `count` 字段。
- 去掉了报告里的重复说明。

**回归结果**

| 范围 | 结果 |
|---|---|
| 单元测试 | 466 passed、1 skipped |
| 集成测试（工作流、记忆、Portal、评测、Store、API、规划） | 56 + 39 passed |
| e2e（规划、记忆、规则意图、子任务、恢复、分析、验证、评测） | 3 + 29 passed |
| 全量回归 | 见第 6 节 |

## 5 负责人现场验证（服务器，真实模型 deepseek-flash）

先用新代码重启 API 和 Worker，再建一条仓库级记忆（`4671e9ad`），内容是“金额用整数分；CSV 金额列输出两位小数字符串”。

| 任务 | 方式 | 关键证据 | 结论 |
|---|---|---|---|
| `0fbcac87` | `plan=auto` | `memory.injected` 在 #3，排在 `plan.started` #6 之前；规划 Agent 在 #13 读取了 `.sca/context/MEMORY.md`。模型判断任务太小，没有拆分；主 Agent 在 #40 读取了记忆。结束后产生 3 条记忆提案 | **规划阶段已能拿到记忆** |
| `7d15bd53` | 显式 3 个子任务 | `memory.injected` 在 #3。`validation`、`totals`、`report` 三个子任务在第 1 轮分别读取了 `MEMORY.md`（#21、#25、#29）。合并后的补丁里，`report.py` 用 `divmod(cents, 100)` 把金额格式化为两位小数，并配了相应测试。补丁中没有 `.sca/` 文件。任务 SUCCEEDED | **子任务已能拿到记忆，产出代码遵守约定** |

这次三个子任务都没有写记忆提案（提示词里已经告诉它们可以写）。所以“子任务提案收集”只有集成测试 `test_planning_context.py` 覆盖，现场还没触发。

## 6 全量回归

运行 `pytest -m 'not codex and not live and not gateway' tests`，结果为 **734 passed、37 skipped、1 failed**（653 秒）。

失败的是 Y 新写的检查点测试：测试仓库没有把 `.sca` 列为忽略项，先执行 `rm --cached` 后，`add -A` 又把它加了回来。修法是调换顺序，先 `add -A`，再 `rm --cached .sca`。修复后，这个测试文件 13 passed；受影响的 e2e（规划、记忆、子任务、fake 链路）9 passed；overlay 相关用例 15 passed。

后续 Mac 测试按 [coverage-test-plan.md](coverage-test-plan.md) 执行。
