# 全量覆盖测试计划（给 Mac 上的 agent 执行）

目标：在 Apple 芯片 Mac + Docker Desktop 上，把架构图 64 个模块中能在本机覆盖的部分全部跑一遍。每个结论都要有运行 ID 和事件序号作为证据。
背景与评审：[mac-run-review.md](mac-run-review.md)。启动方式：[verified-scenarios.md](verified-scenarios.md) 第二部分。

## 规则（必须遵守）

1. **不要读取或打印 `.env`、`.data/portal.env` 的内容**。令牌由脚本自己读取。
2. **失败时不要改代码，也不要进运行中的沙箱手工修**。把失败用例、运行 ID、相关事件序号和 `E ` 报错行原样记下来，然后继续下一个场景。上次 P8 那种“临时容器补丁”会污染证据。
3. 真实模型的结果和脚本化 Agent 的结果分开记。
4. 费用要写合计，包括规划和全部子任务，并注明口径。
5. 每个场景结束后跑一次覆盖报告，把输出贴进汇报。

## S0 准备（约 20 分钟）

```bash
cd ~/code/SuperCloudAgents
git pull
make portal-setup          # 会额外构建 sca-sandbox-analysis:latest
make portal-up && make portal-status
mkdir -p .data/pytest
.venv/bin/ruff check src tests scripts
.venv/bin/pytest -q -p no:cacheprovider --basetemp=.data/pytest/u tests/unit
python3 Milestone/validate.py
python3 scripts/coverage_report.py --jsonl Milestone/records/2026-11-19-order-events.jsonl   # 离线自检
```

覆盖报告的用法（每个场景后都跑一次）：

```bash
python3 scripts/coverage_report.py --since 2026-11-20T00:00:00Z --out .data/coverage-latest.md
# 只看某几个任务或评测：--tasks <任务ID> ...  --runs <评测运行ID> ...
```

它会逐个检查 64 个模块，标出四种状态：已覆盖（附任务 ID 和事件序号）、只能人工检查（附检查命令）、未覆盖（附覆盖方法）、未实现。

修改开关后要重启平台：先 `make portal-down`，再 `<开关>=值 make portal-up`。

## 场景总表

| 场景 | 覆盖的模块 | 开关 | 预计用时 |
|---|---|---|---|
| S1 记忆读写闭环 + 拆分任务 | `memory`、`planner`、`decouple`、`subtask_q`、`executor`、`audit`、`ui_approve`、`vector_store`（需要 embeddings 模型） | 无 | 25 分钟 |
| S2 系统提示词自演进闭环 | `evo_trigger`、`evo_goal`、`evo_eval`、`evo_improve`、`evo_release`、`opencode_versions`、`trajectory_checks`、`obs_risk`、`portal_loop` | 无 | 30 分钟 |
| S3 OpenCode 源码修复闭环（原 V4） | 同 S2，另加 `ui_confirm`、`repos` | 无 | 25 分钟 |
| S4 pi 后端 | `pi_backend` | 评测中心选 pi | 10 分钟 |
| S5 准入 + 限流 | `admission_control`、`gateway_limiter`、`model_gateway`、`sandbox_sched` | `SCA_GATEWAY_LIMITER=true` | 15 分钟 |
| S6 快照分叉 / overlay | `workspace_forks`、`workspace_overlay`、`cache_gw` | `SCA_WORKSPACE_SNAPSHOTS=true`、`SCA_WORKSPACE_OVERLAY=true` | 15 分钟 |
| S7 handd + 空闲冻结 | `handd`、`idle_suspend` | `SCA_SANDBOX_HANDD=true`、`SCA_SANDBOX_SUSPEND_IDLE=true` | 15 分钟 |
| S8 出口安全 | `net_sec` | 无 | 10 分钟 |
| S9 依赖与代码缓存 | `cache_dep`、`cache_code` | 无 | 10 分钟 |
| S10 数据分析 | `analysis_kind` | 无 | 10 分钟 |
| S11 观测与密钥 | `obs_trace`、`obs_replay`、`kms` | obs、openbao profile | 20 分钟 |
| S12 可选：多 Worker、kind | 多 Worker 分布、`k8s` | 第二个 Worker / kind | 40 分钟 |
| 不在 Mac 上覆盖 | `hand_brain`、`brain_split`、`brain_pool`（只支持 Linux 宿主机，已在服务器上验证）；`tool_domain`、`knowledge` 的企业文档部分（未实现）；`design_in`、`design_spec` 的设计稿接入（未实现） | — | — |

---

## S1 记忆读写闭环 + 拆分任务（本次修复的重点）

**目的**：验证三件事。

- 记忆在项目开始时就读取，规划和每个子任务都能用上。
- Agent 提出的记忆经过人工采纳后，下一个任务能命中。
- 记忆的修改有审计记录。

步骤：

1. Portal →「共享记忆」→ 新建一条，作用域填 `repo:<订单示例仓库地址，与提交任务时的 repo 一致>`（也可以用 `tenant` 或 `kind:coding`）：
   - 类别：`convention`
   - 内容：「订单金额统一使用整数分，按币种分别汇总；CSV 金额列输出为两位小数字符串。」
2. 用上次的订单示例仓库提交同样的订单任务，打开“自动拆分”和“拆分审核”。
3. 在任务进度里核对：
   - `memory.injected` 的序号**小于** `plan.started`，并且 `count ≥ 1`。
   - 规划说明卡和拆分结果遵守“整数分”约定。
   - 每个子任务的 `agent.tool`（read）里，能看到读取 `.sca/context/MEMORY.md`。如果模型没读，记下来，但不判失败。
   - `report` 子任务输出的金额格式符合约定（看最终 diff）。
4. 审核时，如果出现“记忆提案”列表，勾选一条合理的提案并批准。没有提案就记“无提案”。
5. 回到「共享记忆」：被采纳的提案状态变为 active，来源指向本任务。
6. 再提交一个小任务，范围是同一仓库，例如“给 totals 加一个空列表的测试”。确认 `memory.injected` 的 count 包含刚采纳的那条。
7. 在「共享记忆」里停用一条记忆，再提交一次。确认停用的那条不再注入。

**期望**：步骤 3、5、6、7 全部成立。

`vector_store` 只有在配置了 `SCA_MEMORY_EMBEDDING_MODEL` 时才算覆盖。没有配置就写“仅关键词召回”。

## S2 系统提示词自演进闭环（接着上次 4/6 的基线）

1. 评测中心打开上次的基线运行。如果在新机器上，就用 `opencode-prompt` 套件、重复 2 次重跑一个基线。
2. 用例矩阵 → `csv-escape` →「用于提示词优化」，填写优化目标：要求把需求中的边界条件变成可执行检查，空字段和特殊字符必须能完整还原。
3. 「查看提示词改动」：确认只改了 `packages/core/src/session/runner/prompt/system.txt`。批准并构建，记下新版本号。
4. 用新版本按原条件重跑，记录逐题结果、费用、耗时。
5. 「用本轮回归继续第 2 轮」至少点一次，确认谱系里新一轮的基线是 B 的提交。

**期望**：
- 构建出新版本，版本记录里有提示词的 SHA-256。
- 版本对比给出结论。

模型不一定能修好 `csv-escape`。没修好也如实记录，这不算平台失败。只有构建失败、对比缺数据或谱系错误，才算平台失败。

## S3 OpenCode 源码修复闭环（原 V4）

按 [verified-scenarios.md](verified-scenarios.md) 第四部分的五步做：基线 5/6 → 修复任务 → 审核 → 构建 → 重跑 6/6。期望和 [verification-flow.md](verification-flow.md) 的 V4 表一致。

## S4 pi 后端

评测中心后端选 pi，套件 `opencode-demo`，其余不变。期望 6/6，或与 OpenCode 基线相当。

## S5 准入 + 限流

```bash
make portal-down && SCA_GATEWAY_LIMITER=true make portal-up
```

1. 评测并发设为 3，同时再手动提交 2 个普通任务。
2. 覆盖报告里应该出现 `admission.waiting`（有排队）和 `admission.granted`。
3. 人工检查限流代理的计数：`curl -s http://127.0.0.1:4100/metrics | grep -i request`。调用次数应大于 0，上游 429 应为 0。

## S6 快照分叉 / overlay

```bash
make portal-down && SCA_WORKSPACE_SNAPSHOTS=true make portal-up
```

同一个带 setup 的任务连续提交两次。第二次应出现 `workspace.forked`，setup 被跳过或明显变快。记录两次的 setup 耗时。

然后改用 `SCA_WORKSPACE_OVERLAY=true` 再跑一次：

- 出现 `workspace.overlay` 事件，就算覆盖。
- 在 Docker Desktop 上报错，就把报错原样记为平台限制。

## S7 handd + 空闲冻结

```bash
make portal-down && SCA_SANDBOX_HANDD=true SCA_SANDBOX_SUSPEND_IDLE=true make portal-up
```

跑一个普通任务，在运行中执行：`docker ps --format '{{.Names}} {{.Status}}' | grep sca-`

- **handd**：在沙箱里执行 `docker exec <沙箱> ps aux | grep handd`，应该能看到 handd 进程。
- **空闲冻结**：留意是否出现 `sandbox.transition` 事件，或者 `docker ps` 里的 `(Paused)` 状态。没有出现也如实记录：冻结只在 Agent 等待模型时触发，非 brain 模式下可能不发事件。

## S8 出口安全

提交一个普通编码任务，指令是：「在 verify.py 里用 urllib 访问 https://example.com 并打印状态码，然后运行它」。

**期望**：沙箱里的访问被代理拒绝，从 403 或连接错误可以看出来。安装 `pypi.org` 的包仍然成功。把拒绝的那条 `agent.command` 序号记下来。

## S9 依赖与代码缓存

同一仓库、同一个 `setup_command`（例如 `pip install requests`）提交两次，对比两次 `setup.completed` 的耗时。第二次应该明显更快。

## S10 数据分析

Portal →「数据分析」：

1. 上传一个小 CSV（订单示例的 20 行即可）。
2. 问题填“按币种汇总金额并给出最大订单”，检查项填一条可执行检查。
3. 期望：任务成功，能看到报告产物，结论数字与手算一致。

## S11 观测与密钥

```bash
make portal-down
# 在 .env 之外以环境变量方式传入，不要编辑 .env 的密钥项
SCA_OTLP_ENDPOINT=http://localhost:4318 make portal-up
"$PWD/.data/run/docker" compose --env-file .env -f deploy/docker-compose.yml --profile obs up -d
```

obs profile 的容器使用 `network_mode: host`。在 Mac 上需要 Docker Desktop 4.34 及以上，并在 Settings → Resources → Network 里打开 “Enable host networking”。打不开就把 S11 记为平台限制。

跑一个任务后：

- **Jaeger**：打开 `http://localhost:16686`，能按 task_id 查到 trace，就算 `obs_trace` 覆盖。
- **Grafana**：`http://localhost:3000` 能看到 Worker 指标面板。

OpenBao 是可选项：`--profile openbao`，并设置 `SCA_SECRET_PROVIDER=openbao`。具体步骤见 `docs/handoff.md`。做不了就跳过并说明原因。

## S12 可选

- **多 Worker**：另开一个终端，用同样的环境变量再起一个 Worker（见 `bash scripts/portal_demo.sh` 的 worker 启动命令）。然后提交一个拆分任务，在 Temporal 历史或事件里确认，不同子任务由不同 Worker 领取。
- **kind**：Docker Desktop 内存需要 ≥ 8 GB。按 `deploy/kind/README.md` 启动两节点集群加 Calico，然后执行：

  ```bash
  SCA_TEST_EGRESS=1 .venv/bin/pytest -q tests/integration/test_k8s_sandbox.py
  ```

## 汇报模板

```text
机器：<芯片 / macOS / Docker Desktop 版本 / 分配内存>
提交：<git log --oneline -1>
S0：ruff <> / 单元 <> / validate <>
S1：任务 <ID>；memory.injected #<n> < plan.started #<m>，count=<k>；采纳 <记忆ID>；第二任务 <ID> 命中 <k>；停用后 <结果>
S2：基线 <运行ID> <x/y> → 候选 <任务ID> → 版本 <sca.N> → 重跑 <运行ID> <x/y>，结论 <...>
S3–S11：<每项一行：运行/任务 ID + 关键事件序号 + 结果>
覆盖报告：covered <X>/64，manual <Y>，planned <Z>（附 .data/coverage-latest.md）
费用合计：<美元>（口径：含规划与子任务）
失败与平台限制：<场景 + ID + E 行>
```
