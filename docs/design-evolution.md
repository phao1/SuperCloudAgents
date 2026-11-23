# OpenCode 自演进（Phase 5）设计

目标：平台**自动、持续地**优化 OpenCode 的系统提示词和工具（工具指引、工具描述，必要时也改工具代码），让 OpenCode 在编码任务上的效果持续提升。每一步都有评测证据，可以回滚，人可以随时介入。

## 1 现状与差距

已有：一条**人工驱动的单轮闭环**。

1. 跑评测（`opencode-prompt` 3 题 / `opencode-demo` 6 题）。
2. 人挑一个失败用例。
3. 生成一个“改 `system.txt`”的候选任务（OpenCode 改自己的源码）。
4. 人审核后构建新二进制（约 2.5 GB 内存，串行）。
5. 重跑，做配对比较（符号检验）。
6. 可以继续下一轮（有谱系）。

| 差距 | 影响 |
|---|---|
| **每个候选都要完整构建 OpenCode**：提示词编译进了二进制 | 一代只能试 1 个候选，一次要几十分钟；没法做多候选、多代的搜索 |
| **诊断只看单个用例**，只取最后 8 条命令 | 改动容易只针对这一题过拟合，看不到跨用例的共性问题 |
| **基准太小**：3 + 6 题，也没有开发集/留出集之分 | 统计上分不出好坏（符号检验至少需要 6 对）；在同一批题上调参再在同一批题上验证，会把过拟合误当成进步 |
| **只能改 `system.txt`** | 工具指引、工具描述这些对效果影响很大的部分，没有被纳入演进 |
| **没有自动控制器**：每一步都要人点 | 不能“持续”演进 |
| **没有冠军版本**，也没有实验日志 | 不知道“当前最好”是哪个版本，同样的失败想法会被反复尝试 |

## 2 设计

### 2.1 两条通道

| 通道 | 改什么 | 怎么评测 | 用途 |
|---|---|---|---|
| **快通道**（新增） | 系统提示词、工具指引块、各工具的描述 | **不构建**。用一次性构建的“可覆盖版本”OpenCode，运行时从文件加载覆盖内容（`agent_overlay`） | 演进搜索的主力：每代多个候选，跑多代 |
| **慢通道**（已有） | 工具代码、运行逻辑 | 源码修复任务 → 构建 → 版本注册 | 修真实缺陷；把冠军覆盖内容“固化”进正式版本 |

快通道的实现分两步：

- 先给 OpenCode 打一个小补丁：环境变量指向的文件存在时，用文件内容替换内置的系统提示词、工具指引和工具描述；文件不存在时，行为与原版完全相同。打好补丁后构建一次，注册为可覆盖的基线版本。
- 平台侧在任务 spec 里新增 `agent_overlay` 字段，内容为 `{system_prompt?, tool_guidance?, tool_descriptions?}`。Worker 把它写成只读文件挂进沙箱，再通过环境变量把路径交给 OpenCode。

### 2.2 演进控制器（Temporal 工作流 `EvolutionWorkflow`）

每一代（generation）按以下步骤执行：

1. **评测冠军**：在开发集（dev）上跑当前冠军，重复 R 次。冠军的结果可以跨代复用。
2. **反思诊断**：汇总全部失败和低分样本的轨迹，包括轨迹规则命中、命令、报错、验收输出。用 LLM 归纳出共性失败模式，输出 1–3 条结构化诊断。只用开发集的证据，留出集对提议者完全不可见。
3. **提出候选**：基于“当前覆盖内容 + 诊断 + 实验日志（已经试过什么、结果如何）”，由 LLM 生成 K 个候选覆盖内容。每个候选必须说明假设、改了什么、预期哪类用例受益。
4. **静态检查**：长度上限；必须保留占位符；不能出现基准题的文件名、函数名或答案（与用例文本做 n-gram 比对）；不能改权限相关的内容。
5. **评测候选**：每个候选在开发集上跑，重复 R 次。
6. **选择**：按通过率排序，通过率相同时比成本和轮数。如果某个候选在开发集上配对优于冠军，就进入留出集门禁。
7. **留出集门禁**：在留出集（holdout）上比较候选与冠军。通过率不降、配对不劣、成本涨幅不超过阈值，才**晋升**为新冠军。否则记入日志并作废。
8. **写实验日志**：记录每个候选的假设、差异、开发集和留出集结果、是否晋升。下一代的提议者会读这份日志。

停止条件（满足任一）：
- 达到代数上限或预算上限；
- 连续 N 代没有晋升（平台期）；
- 人工停止。

可以选“晋升前需人工批准”，Portal 会给出差异和证据。

### 2.3 基准

- 新增 `evals/opencode-evolve`：不少于 30 题，覆盖修 bug、加功能、重构、多文件改动、补测试、边界条件、Unicode、大文件编辑或搜索、需要多次验证的长任务。
- 每题都有隐藏验收，并在 `task.yaml` 里标注 `split: dev | holdout`，比例约 2:1。
- 评测请求可以按 `split` 过滤。
- 后续可以把真实任务的失败“收割”成新用例，属于 Phase 5b。

### 2.4 冠军与固化

- 冠军注册表记录：覆盖内容、所属基线版本、开发集和留出集成绩、谱系（父冠军、所在代）。
- 普通任务可以选择使用冠军覆盖内容（选项 `use_champion`），默认不变。

#### 固化与默认版本

部署时执行数据库迁移 `0017`，更新 API 和 Worker。Worker 使用已配置的 `SCA_EVAL_API_TOKEN`
提交评测任务。在已有的 OpenCode 裸仓库上，可运行
`python scripts/opencode_overlay_upgrade.py` 为 `main` 添加编译时覆盖支持。新安装会应用两层补丁。

使用 `eval` 租户的管理员令牌提交固化，接口返回 HTTP 202 和构建记录。Worker 从冠军基线版本
的提交创建 `sca/champion-<短 ID>` 分支，把覆盖内容写入 `sca-default-overlay.json`，再通过常规
构建流程注册新版本。运行时按字段选用环境变量所指文件中的覆盖、编译时覆盖、OpenCode 内置文本。
工具描述按工具名选用，基线已有的编译时覆盖也按同一优先级继承。已支持运行时覆盖的旧基线会在
固化提交中补上编译时覆盖支持。

```bash
curl -fsS -X POST "$SCA_API_URL/v1/champions/$CHAMPION_ID/solidify" \
  -H "Authorization: Bearer $SCA_API_TOKEN" \
  -H "Content-Type: application/json" --data '{"notes":"固化冠军覆盖"}'
curl -fsS "$SCA_API_URL/v1/champions/$CHAMPION_ID" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
```

构建成功后会评测两次完整的原始套件，包含全部分组。两次都沿用原评测的模型、重复次数、
并发度和单题预算，每次的总预算覆盖该套件的全部重复用例。套件变更时，恢复原内容后再提交。
从 `solidify_builds` 中取得 `overlay_eval_run_id`（基线版本加运行时覆盖）和
`solidified_eval_run_id`（新版本），等 `regression_status` 为 `completed` 后在评测对比接口查看
逐题通过率、轮数和成本。管理员确认结果后，可将已构建的版本设为平台默认版本。

```bash
curl -fsS "$SCA_API_URL/v1/evals/compare?a=$OVERLAY_RUN_ID&b=$SOLIDIFIED_RUN_ID" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
curl -fsS -X POST "$SCA_API_URL/v1/agents/opencode/versions/$VERSION/default" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
```

OpenCode 任务首次分配沙箱时，优先使用任务指定的 `agent_version`，其次使用平台默认版本，
最后使用 `SCA_OPENCODE_DEFAULT_VERSION`。任务在后续轮次继续使用首次选定的版本。
要切回上一版本，用同一接口指定该版本。

本地测试使用临时裸仓库和模拟构建、评测，覆盖基线提交、评测配对及版本选择。Bun、实际构建和
模型评测尚待现场验证。Postgres 集成测试在连接受限的沙箱中跳过。

## 3 分工（分支 `evolve`）

| Worker | 内容 | 主要文件 |
|---|---|---|
| A 覆盖通道 | OpenCode 覆盖补丁；setup 和升级脚本；`agent_overlay` 字段从 spec 到沙箱的完整链路；冠军覆盖的读取 | `deploy/opencode/`、`scripts/opencode_setup.py`、`src/sca/agents/opencode.py`、`src/sca/kernel/spec.py`、`src/sca/core/models.py`、`src/sca/kernel/activities.py` |
| B 基准 | `evals/opencode-evolve`（≥30 题，dev/holdout）；套件加载与评测请求的 `split` 过滤 | `evals/opencode-evolve/**`、`src/sca/eval/suites.py`、`src/sca/eval/activities.py`、`src/sca/api/evals.py` |
| C 控制器 | `src/sca/evolve/` 工作流与活动（反思、提议、静态检查、选择、门禁、日志）；数据库迁移；`/v1/evolutions` API；冠军注册表 | `src/sca/evolve/**`、`src/sca/db/migrations/versions/0015_*`、`src/sca/db/schema.py`、`src/sca/api/evolutions.py` |
| D（第二批） | Portal 演进视图（代数曲线、候选、谱系、晋升/回滚）；冠军固化到正式版本 | `portal.html`、`api/agents.py` |

## 4 退出条件（负责人现场验证）

1. 用可覆盖版本跑 `opencode-evolve` 开发集，结果与同一基线下不带覆盖的运行一致（覆盖为空时行为不变）。
2. 一次真实演进，`deepseek-flash`，3 代 × 3 候选 × 2 次重复，预算 ≤ 3 美元：
   - 产生完整的实验日志；
   - 至少有一次真实的晋升或拒绝决定，并且有留出集证据；
   - 全程没有构建 OpenCode。
3. 已有回归全部通过；默认行为不变（不传覆盖内容时，任务与现在完全相同）。

## 6 产品化

部署时应用迁移 `0016`，再重启 API 和 Worker。令牌通过环境变量提供。下列示例用
`$TOKEN` 表示租户管理员令牌，用 `$EVAL_ADMIN_TOKEN` 表示 `eval` 租户管理员令牌。

### 6.1 任务使用冠军与灰度回滚

任务规格中的 `agent_overlay` 优先。普通 OpenCode 任务省略 `use_champion` 或传
`null` 时，按任务 ID 的 SHA-256 值取模 100 进行灰度分桶。系统先查租户在该版本和
模型上的灰度，再查 `eval` 租户的平台默认值。没有配置或分桶未命中时使用基线。
`use_champion: true` 使用当前活跃冠军，`false` 跳过冠军。未选择冠军的常规评测默认
测量基线。任务启动时固定选择，`task.result` 和 `champion.applied` 事件记录冠军 ID
与覆盖内容的 SHA-256。

```bash
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind":"coding","spec":{"repo_url":"https://example.com/repo.git","instruction":"修复解析边界",
       "agent_backend":"opencode","model":"deepseek-flash","verify_command":"pytest -q","use_champion":true}}' \
  "$API/v1/tasks"
curl -H "Authorization: Bearer $TOKEN" "$API/v1/champions/rollouts"
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"agent_version\":\"2.3.0-sca.1\",\"model\":\"deepseek-flash\",\"champion_id\":\"$CHAMPION_ID\",\"percent\":20}" \
  "$API/v1/champions/rollouts"
curl -X POST -H "Authorization: Bearer $EVAL_ADMIN_TOKEN" \
  "$API/v1/champions/$CHAMPION_ID/rollback"
```

灰度接口的范围是令牌所在租户。使用 `eval` 管理员令牌设置平台默认值，使用
`GET /v1/champions` 查看冠军 ID。回滚会激活父冠军，并逐条把指向原冠军的所有租户
灰度改为指向父冠军，保留各自百分比。操作记录在 `evolution_events`。

### 6.2 定期评测与自动演进

Worker 启动时注册 Temporal 定时任务，每 5 分钟检查到期计划。Worker 使用
`SCA_EVAL_API_TOKEN` 提交评测任务。计划要求套件同时包含 `dev` 和 `holdout`，
并在 `dev` 集评测当前活跃冠军，首个冠军晋升前测量基线。评测完成后，通过率低于
`threshold`、上次同一套件和版本模型的样本本次从通过变为失败（按用例和重复序号
配对），或启用 `always`，任一条件满足就启动演进。演进单独使用计划中的预算，
并沿用留出集门禁和可选的人工审批。配置 `auto_rollout_percent` 时，每次晋升都会
更新平台默认灰度。

```bash
curl -X POST -H "Authorization: Bearer $EVAL_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"suite":"opencode-evolve","split":"dev","agent_version":"2.3.0-sca.1","model":"deepseek-flash",
       "interval_hours":24,"threshold":0.8,"enabled":true,"always":false,
       "config":{"generations":2,"budget_usd":3},"auto_rollout_percent":20}' \
  "$API/v1/evolution-schedules"
curl -X POST -H "Authorization: Bearer $EVAL_ADMIN_TOKEN" \
  "$API/v1/evolution-schedules/$SCHEDULE_ID/run-now"
curl -H "Authorization: Bearer $EVAL_ADMIN_TOKEN" "$API/v1/evolution-schedules/$SCHEDULE_ID"
```

`GET /v1/evolution-schedules` 列出计划，`PUT /v1/evolution-schedules/{id}` 提交完整
配置，`DELETE /v1/evolution-schedules/{id}` 删除计划。计划详情记录最近一次启动时间、
评测 ID 和触发的演进 ID。`run-now` 可手动执行已停用的计划，同一计划正在评测时返回
`409`。

### 6.3 从失败任务采集评测用例

租户管理员可提交本租户失败或被人工拒绝的真实编码任务。系统优先从可信镜像读取
任务的基线提交，镜像不可用时按原仓库和基线重新获取。候选暂存在
`SCA_DATA_DIR/harvest/<id>/`。可提供独立的 `acceptance_command`，省略时验收会执行
任务中的全部验证命令。选择 `holdout` 的用例只用于留出集，提议模型只接收开发集证据。

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"task_id\":\"$TASK_ID\",\"suite\":\"opencode-harvested\",\"split\":\"holdout\",\"notes\":\"边界条件回归\"}" \
  "$API/v1/evals/harvest"
curl -H "Authorization: Bearer $TOKEN" "$API/v1/evals/harvest"
curl -X POST -H "Authorization: Bearer $EVAL_ADMIN_TOKEN" \
  "$API/v1/evals/harvest/$CANDIDATE_ID/approve?tenant_id=team-a"
```

`eval` 管理员可用 `?tenant_id=team-a` 查看指定租户提交的待审候选，并检查暂存目录。
审批通过后写入 `evals/<suite>/cases/<case-id>/`（根目录可由 `SCA_EVAL_SUITES_DIR`
配置），包含 `task.yaml`、`repo/`、
`acceptance.yaml` 和 `acceptance/`，新套件会同时生成 `suite.yaml`。提交租户的
管理员或 `eval` 管理员可调用 `POST /v1/evals/harvest/{id}/reject` 拒绝待审候选。

### 6.4 验证

单测覆盖任务启动时固定冠军、灰度分桶、定时评测触发条件、采集文件布局和留出集
隔离。迁移 `0016` 已离线生成 SQL。开发沙箱拒绝 Postgres 和 Temporal 连接，真实
数据库的行级安全与定时工作流尚待集成环境验证。

### 6.5 评测套件变更

评测在排队和提交每个用例前校验套件内容。套件根目录的 `README.md` 文档修改不会影响新评测。
用例规格、隐藏验收和仓库内容（包括仓库内的 README）仍参与校验。内容变更或输入校验错误会
结束评测并记录 `status: failed` 和 `reason`。Postgres 和 API 的临时故障继续重试。
评测会停止安排后续批次，当前批次已发起的提交和已提交的任务可以独立完成，失败后不再收集结果。

演进收到失败的评测结果后会结束，并在详情中记录失败原因和对应评测 ID。固化构建的回归结果为
`regression_status: failed` 时，查看 `regression_reason`。并行的另一组固化评测可以独立完成。

```bash
curl -fsS "$SCA_API_URL/v1/evals/runs/$EVAL_RUN_ID" \
  -H "Authorization: Bearer $EVAL_ADMIN_TOKEN"
curl -fsS "$SCA_API_URL/v1/evolutions/$EVOLUTION_ID" \
  -H "Authorization: Bearer $EVAL_ADMIN_TOKEN"
curl -fsS "$SCA_API_URL/v1/champions/$CHAMPION_ID" \
  -H "Authorization: Bearer $EVAL_ADMIN_TOKEN"
```

旧评测的摘要保留在记录中。已有演进继续使用原摘要配对，旧基线复测在验证原摘要后也保留相同
的配对标识。旧冠军的固化先验证原始套件，再用新摘要成对运行覆盖版和固化版评测。如果旧摘要
因根目录 README 改动而无法验证，请恢复原始套件内容或用当前套件重新建立基线。摘要不同的两次
独立运行需要重新评测后才能配对。无需数据库迁移。

相关单测 89 项通过，模拟 I/O 验证了套件中途变更时评测和演进一起结束。Temporal Replayer
通过了两份合成的旧版评测历史回放。当前沙箱拒绝 Postgres 和 Temporal 的套接字连接，
真实服务上的集成测试仍待验证。

### 6.6 模型服务账户故障

升级后重启 API 和 Worker。两者应使用同一个 `SCA_DATA_DIR`，以共享模型网关状态，无需数据库迁移。
模型服务余额或额度耗尽、密钥无效时，Portal 的首页和任务页会显示红色横幅及故障时间。
充值、恢复额度或更新 LiteLLM 的上游密钥后，下一次成功的模型调用会清除横幅。

任务遇到 `billing` 或 `auth` 时立即结束为 `FAILED`。详情的 `state_reason` 给出处理方式，
`model.provider_error` 事件提供类别和可用的 HTTP 状态码。评测详情用 `infrastructure_error`
标记受影响的用例，摘要通过率和配对对比会排除这些用例。评测停止提交后续批次，已经提交的任务
可以独立完成。评测与演进的 `reason` 会保留故障原因。恢复服务后可重新提交任务、评测或演进。

```bash
curl -fsS "$SCA_API_URL/v1/portal/config" -H "Authorization: Bearer $TOKEN"
curl -fsS "$SCA_API_URL/v1/tasks/$TASK_ID" -H "Authorization: Bearer $TOKEN"
curl -fsS "$SCA_API_URL/v1/tasks/$TASK_ID/events" -H "Authorization: Bearer $TOKEN"
curl -fsS "$SCA_API_URL/v1/evals/runs/$EVAL_RUN_ID" -H "Authorization: Bearer $EVAL_ADMIN_TOKEN"
curl -fsS "$SCA_API_URL/v1/evolutions/$EVOLUTION_ID" -H "Authorization: Bearer $EVAL_ADMIN_TOKEN"
```

`gateway_provider_error` 提供最近一次已识别故障的 `kind` 和 UTC 时间 `at`。
`gateway_provider_account_error` 保留账户故障，直到成功调用。`rate_limit` 和 `unavailable`
继续走原有重试流程。

## 7 DeepSeek Harness 自演进（Phase 7）

`scripts/portal_demo.sh up` 的 Docker 和源码模式，以及 Windows 上的
`.\scripts\portal_source.ps1 up`，都会在启动 API 和 Worker 前运行
`alembic upgrade head`，应用已有迁移（含 `0025` 和 `0026`）。手动部署时，先运行同一条迁移命令。
Worker 需要 `dsh` 后端、模型网关和 `SCA_EVAL_API_TOKEN`。在 Portal「自动演进」
选择 DeepSeek Harness、评测套件和模型。
基线版本填写已安装的 `@deepseek-ai/dsh` npm 版本，留空使用 `SCA_DSH_VERSION`（默认
`0.2.2-rc.5`）。已有演进记录、冠军、灰度和定期计划默认归属 OpenCode。

Docker 任务可设置 `"image":"python:3.12"`，或选择与容器架构一致的 glibc Linux 镜像。
镜像需要兼容的 libstdc++、任务所需的工具，以及 SCA 使用的 Bash 和 Git。Agent
启动和沙箱命令包装无需镜像自带 Python 或 Node.js。Alpine/musl 镜像会收到中文错误。
Docker 启动脚本会准备 DSH 运行时，Worker 也会在首次需要时安装。版本化的运行时
位于 `SCA_DATA_DIR/tools/docker-node`，以只读方式挂载。它使用官方 Node.js 24.22.3
镜像安装指定的 npm 包和对应架构的原生依赖，清理未被容器使用的旧版本。
[运行时磁盘配置和旧镜像清理](../README.md#docker-运行时磁盘空间)。
[Node.js 24 Linux 二进制要求](https://github.com/nodejs/node/blob/v24.22.3/BUILDING.md#official-binary-platforms-and-toolchains)

DSH 在每轮通过 `headless --patch` 载入附加提示词。它保留内置系统提示、工具定义和权限策略，
将 `system_prompt`、`tool_guidance` 及按工具名组织的 `tool_descriptions` 追加到系统提示。
提议者只编辑追加的文本。基线提供已知版本的内置开场白供参考，其余提示由 DSH 插件在运行时组装。
配置写入沙箱中的临时目录，任务仓库无需添加指令文件。
[DSH 系统提示组装](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.5/packages/core/system-prompt/README.zh.md)

### 最小试跑

使用仓库已提交的 `evals/dsh-smoke` 套件，内含两个开发用例和一个留出用例。
套件根目录默认为 `evals`。如果自定义了 `SCA_EVAL_SUITES_DIR`，请在 API 和 Worker
上使用同一个包含 `dsh-smoke` 的套件根目录。

用 `eval` 租户管理员令牌提交一代、两个候选、每题一次、串行的演进。先评测开发集，有候选
通过开发集门禁才评测留出集。下例把编码评测开支限制在 0.20 美元，每题最多 0.02 美元。
反思与提议的模型调用单独计费。

```powershell
$headers = @{ Authorization = "Bearer $env:SCA_API_TOKEN" }
$body = @{
  suite = "dsh-smoke"
  agent_backend = "dsh"
  model = "deepseek-flash"
  generations = 1
  candidates = 2
  repeat = 1
  concurrency = 1
  budget_usd = 0.20
  case_budget = 0.02
}
$run = Invoke-RestMethod "$env:SCA_API_URL/v1/evolutions" -Method Post `
  -Headers $headers -ContentType "application/json" -Body ($body | ConvertTo-Json)
Invoke-RestMethod "$env:SCA_API_URL/v1/evolutions/$($run.id)" -Headers $headers
```

Portal「演进记录」按后端显示运行详情。单独跑开发集时，向 `POST /v1/evals/runs` 提交
`{"suite":"dsh-smoke","split":"dev","variant":{"agent_backend":"dsh","model":"deepseek-flash"},"budget":0.04,"case_budget":0.02}`。
定期演进使用 `POST /v1/evolution-schedules`，设置顶层 `"agent_backend":"dsh"` 与
`"split":"dev"`，在 `config` 中传入代数、候选和预算配置。`opencode-prompt` 是专用的
OpenCode 提示词评测入口；有开发集和留出集的 `opencode-evolve*` 套件可用于 DSH。

任务因 Agent 或环境故障结束且未完成一轮时，用例标记为 `errored`。DSH 预算拒绝有单独的判定规则。
评测详情返回 `errored` 计数及逐题状态。超过半数已执行用例出错时，评测以 `failed` 结束并记录原因。
演进需要完整的配对结果。基线无效会结束演进，候选评测无效会记录拒绝原因。
效率晋升要求双方有正的实测用量，已定价模型比较美元支出，未定价模型比较总 Token。
开发集通过率持平或提高，留出集通过率不降且用量不涨。
Portal 的评测和演进详情显示失败原因，演进活动失败时还会显示错误类别。
可通过 `GET /v1/evals/runs/{id}` 和 `GET /v1/evolutions/{id}` 查看这些字段。
查看单个任务的实测费用时，读取 `GET /v1/tasks/{id}` 的 `usage.cost_micro_usd`。
Token 用量和轮次费用可在任务的 `agent.usage` 与 `agent.turn_completed` 事件中核对。

冠军按后端、版本和模型分别管理。使用 `GET /v1/champions?agent_backend=dsh` 查看冠军。
访客模式开启时，可在本机直接读取 DSH 冠军详情。创建者、凭证字段和提示词里的敏感文本会脱敏。

```bash
curl -fsS "http://127.0.0.1:8000/v1/champions/$CHAMPION_ID"
```

在 Portal 提交 DSH 任务时选择「冠军覆盖 → 使用当前冠军」，或在 `POST /v1/tasks` 的
`spec` 中加入 `"agent_backend":"dsh","use_champion":true`。要配置灰度，在
`PUT /v1/champions/rollouts` 的请求中加 `"agent_backend":"dsh"`。DSH 冠军即时可用，
调用固化接口会返回 HTTP 200 和说明。

### DSH 单题预算

Worker 的 `SCA_DSH_MAX_OUTPUT_TOKENS` 默认为 `8192`，适用于 Messages、Chat Completions
和上下文压缩请求。修改环境变量后重启 Worker。Headless 模式关闭 `session-title-llm` 的后台模型请求。
按仓库配置的峰值费率，`deepseek-flash` 的 8,192 个输出 Token 约为 $0.00983，预留时还会
计入输入费用。`deepseek-v4-pro` 的输出费率更高，使用小额 `case_budget` 时需相应降低输出上限。
[DSH Messages 默认值](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.5/packages/llm/llm-deepseek/src/defaults.ts)

LiteLLM 1.110.2 按估算输入费用加最大输出费用预留金额，输出上限取请求值和模型上限中的较小值。
有剩余额度时，默认机制可把超大的预留缩到剩余额度，导致同一虚拟密钥的并发请求暂时被拒。
Messages 流的成功回调在取得最终用量后按实测费用结算，多余预留随之释放，回调可能晚于客户端收到响应。
失败路径会清理预留，取消时可能保留已发出的输入费用。平台保留预留机制以约束并发开支。
启用 `disable_budget_reservation` 后并发请求可能透支。`fail_closed_budget_enforcement` 会直接拒绝
估算费用超过剩余额度的请求。
[金额预留与释放](https://github.com/BerriAI/litellm/blob/v1.110.2/litellm/proxy/spend_tracking/budget_reservation.py)
[流式请求处理](https://github.com/BerriAI/litellm/blob/v1.110.2/litellm/proxy/common_request_processing.py#L3269-L3294)
[费用回调](https://github.com/BerriAI/litellm/blob/v1.110.2/litellm/proxy/hooks/proxy_track_cost_callback.py#L1195-L1254)

OpenCode 和 Pi 的网关配置走 Chat Completions，分别按模型目录及上下文余量、Pi 自定义模型的
16,384 Token 默认值及上下文余量设置输出上限。Codex 走 Responses，其请求体省略输出上限时
由 LiteLLM 估算。启用 SCA 限流代理时，默认的 `auto` 协议为 DSH 选用 Chat Completions。
代理按请求体大小和输出上限预留 TPM，读到流中的 `usage.total_tokens` 后校正。LiteLLM 继续
执行独立的美元预算检查。
[OpenCode 请求构建](https://github.com/anomalyco/opencode/blob/v2.3.0/packages/core/src/session/model-request.ts)
[Pi 模型默认值](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/src/core/provider-composer.ts#L220-L237)
[Pi 请求设置](https://github.com/earendil-works/pi/blob/v1.5.2/packages/ai/src/api/simple-options.ts#L44-L74)
[Codex 请求结构](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/common.rs#L382-L412)

模型已有响应后遇到预算拒绝时，评测计入一个失败用例并保留配对，`results[].reason` 为「预算耗尽」。
任务的首个模型请求就被拒时，评测以配置错误结束，`reason` 提示检查预算和输出上限。可读取评测详情
和任务事件核对分类与用量。

```bash
curl -fsS "$SCA_API_URL/v1/evals/runs/$EVAL_RUN_ID" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
curl -fsS "$SCA_API_URL/v1/tasks/$TASK_ID/events" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
```

### 实时进度与费用

在「自动演进」中选择评测模型和「反思与候选生成模型」。两个选择器读取
`GET /v1/models`，启动接口用 `model` 和可选的 `evolve_model` 指定对应模型。
反思摘要与候选假设使用简体中文，给 Agent 的覆盖内容可保留英文。[模型接入](models.md)

运行中的演进和代际节点显示「进行中」。完成的评测按已收集、可评分的结果计算通过率。
顶部「已用」随 DAG 刷新，包含各次评测已经记账的任务费用，以及 LiteLLM 随反思和提议响应
返回的费用。页面另列评测预算。DAG 根节点的 `cost_usd` 为总额，`meta.eval_spent_usd` 和
`meta.proposer_spent_usd` 提供明细。[LiteLLM 响应费用字段](https://github.com/BerriAI/litellm/blob/v1.110.2/litellm/proxy/common_request_processing.py#L1904-L1928)

```bash
curl -fsS "$SCA_API_URL/v1/models" -H "Authorization: Bearer $SCA_API_TOKEN"
curl -fsS "$SCA_API_URL/v1/evolutions/$EVOLUTION_ID/dag" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
curl -fsS "$SCA_API_URL/v1/evolutions/$EVOLUTION_ID/messages" \
  -H "Authorization: Bearer $SCA_API_TOKEN" --json '{"mode":"next","text":"优先验证边界条件"}'
curl -fsS "$SCA_API_URL/v1/evolutions/$EVOLUTION_ID/messages" \
  -H "Authorization: Bearer $SCA_API_TOKEN" --json '{"mode":"now","text":"本代改为优先检查空输入"}'
```

### 对话与审批

演进详情的「对话」显示候选假设、提议依据、覆盖变更概况与开发集及留出集的评测结果。
桌面端的对话面板与 DAG 并排，消息可单独滚动。屏幕宽度为 900 像素或更窄时，DAG 排在对话上方。
任务对话提供「排队发送」和「打断并发送」。输入框随内容增高，最多显示六行。运行结束后，
底部显示「任务已结束」或「演进已结束」。[任务插话使用说明](operations.md#运行中插话)

演进对话可选择「下一代生效」（`next`，旧 API 的 `guidance` 仍可用）或「立即生效」（`now`）。
立即生效会中止本代剩余的评测，结算已启动用例的费用，随后沿用同一代编号重新提议。
未完成决策的旧候选显示「已因指引中止」，已完成的基线和已晋升的冠军可继续复用。
达到剩余预算下限时结束演进。暂停期间发送的指引会在恢复后使用。新版 Worker 启动的演进支持立即生效。
首次基线会先完成。已发出的反思与提议请求会返回，替换时跳过这些提议对应的评测。
提议活动开始使用指引时，消息即可显示「已应用于第 N 代提议」。

待审卡片固定在消息区顶部，也可通过侧栏「待审批」打开。展开「变更与审核详情」可查看文件和补丁，
点击「要求修改」可填写并提交修改意见。提交决定后在消息列表查看带时间的简短记录。
`eval` 租户管理员可在演进卡片上批准或拒绝候选晋升。

侧栏在提交审批、切换页面和定期轮询时更新待审数量。「本机免令牌」和租户令牌登录均可使用待审批入口。
`GET /v1/approvals` 返回当前租户可查看的待审事项、单行标题与 Portal 跳转链接。任务事项还提供
`task_kind`、`backend` 与可用的 `project_name`。访客可在脱敏的任务或演进对话中查看只读审批卡片，
待审批入口隐藏且接口返回 `401`。归档 Portal 隐藏待审批入口，可从「任务进度」回放已发布的记录。

```bash
# 本机免令牌模式
curl -fsS http://127.0.0.1:8000/v1/approvals
# 令牌登录
curl -fsS "$SCA_API_URL/v1/approvals" -H "Authorization: Bearer $SCA_API_TOKEN"
curl -fsS "$SCA_API_URL/v1/evolutions/$EVOLUTION_ID/conversation?limit=50" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
curl -fsS "$SCA_API_URL/v1/tasks/$TASK_ID/review" \
  -H "Authorization: Bearer $SCA_API_TOKEN" --json '{"decision":"request_changes","feedback":"请补充空输入测试"}'
```

任务对话使用 `GET /v1/tasks/{id}/conversation`。可用返回的 `cursor` 继续读取两类对话。

### 验证

在 GitHub Actions 的 **Source mode** 工作流中选择 **Run workflow**，可在 Windows、macOS
和 Ubuntu 上验证源码模式。烟测提交 `fake` 任务后，通过各平台的源码启动脚本导出 ZIP 并重新导入，
确认新增记录为 0，再用 `GET /v1/tasks` 核对原任务。手工操作示例见 [历史导出与导入](history-transfer.md)。

模拟的 headless 进程和网关验证了提示词追加、续接、Token 用量和虚拟密钥费用。
安装器单测模拟了 1,179 MB 的 DSH 安装，覆盖 DSH 与 Pi 的磁盘空间门槛及缓存复用。
评测与演进测试覆盖多数用例出错、基线失效、候选拒绝和零费用门禁。启动测试验证迁移
先于 API 和 Worker，迁移 `0025` 的 SQL 已离线检查。
使用已安装的 DSH 0.2.2-rc.5 和模拟流式响应，核对了两个协议的 `max_tokens` 请求字段和标题请求数。
单测验证了首个请求被拒和中途耗尽预算对应的评测结果。
当前沙箱拒绝 Postgres 和 Temporal 套接字连接。真实服务、Docker 安装体积、Windows 实机
和付费模型仍需现场验证。

对话和待审批测试覆盖六种后端输出、按轮分组、访客脱敏和租户范围，并验证本机免令牌与租户令牌的
审批提交及归档导航。单元回归通过 1,298 项，跳过 27 项。测试进程使用带超时的选择器轮询，
依赖受限套接字或 Chromium 的用例在沙箱中跳过。对话、审批和 Portal 相关集成测试通过 9 项，
跳过 37 项。沙箱拒绝 PostgreSQL 和 Temporal 连接，依赖这两项服务的流程仍待现场验证。

对话布局回归覆盖了插话方式、审核详情展开、审批记录收起、结束提示与路由切换。
模拟工作流仍在等待审批时，待审数量会先更新，任务再次进入审核后会重新计入。
本次单元回归通过 1,309 项，跳过 23 项。相关集成测试通过 3 项，另有 11 项因 PostgreSQL 套接字受限而跳过。
Playwright 布局用例检查 1440×1000 和 390 像素视口。当前沙箱拒绝启动 Chromium，
这两种尺寸仍待浏览器验证。
