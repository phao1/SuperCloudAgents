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
