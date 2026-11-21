# 基础商用验收

在目标部署上，用一个新租户走通运营开户、提交任务、审阅补丁和核对用量。交付的每项能力都应有通过记录。先执行烟测，再按清单补齐依赖客户账号、仓库或外部服务的验证。记录部署版本、日期、烟测输出和对应的任务、PR、工单或发票 ID。

- [ ] 默认烟测无 `FAIL`，租户开通、权限、知识库、用量导出和暂停恢复均为 `PASS`。
- [ ] 用目标部署的 Agent、模型和测试仓库执行 `--with-agent`，确认 `coding`、`coding-review`、`usage` 和 `usage-csv` 均为 `PASS`。
- [ ] 在目标部署补齐启用的 SCM、工具、演进与 Portal 流程的验证。使用 Stripe 模拟器完成付款、webhook 和发票烟测，接入真实收款前用客户凭证在 Stripe 测试模式复验。为每个 `SKIP` 记录原因和待补的证据。

## 运行烟测

准备已完成数据库迁移的 API 和 Worker。用部署密钥将平台管理员令牌注入 `SCA_PLATFORM_ADMIN_TOKEN`，在装有 `httpx` 的 Python 环境运行下列命令。

```bash
.venv/bin/python scripts/commercial_smoke.py --api http://127.0.0.1:8000
.venv/bin/python scripts/commercial_smoke.py --api http://127.0.0.1:8000 \
  --with-agent --repo /srv/sca/smoke.git
.venv/bin/python scripts/commercial_smoke.py --api http://127.0.0.1:8000 \
  --stripe-sim http://127.0.0.1:12111
```

默认模式只检查 API。知识库使用部署配置的检索方式，启用嵌入时会调用嵌入模型。编码模式需要 Worker 能读取一个含有效 `HEAD` 的测试仓库，可以传 Worker 可见的裸仓库绝对路径或 Git 克隆 URL。私库凭证按现有配置提供给 Worker。任务会创建一个带唯一标记的文本文件，执行验证命令并发布补丁，使用部署的默认 Agent 和模型，关闭 Best-of-N 并禁用推送。`--wait-seconds` 可调整每个轮询步骤的时限，默认 600 秒。

脚本创建唯一的 `commercial-smoke-*` 租户，使用 `team` 套餐，凭证只在进程内使用。每步输出 `PASS`、`FAIL` 或带原因的 `SKIP`。失败后跳过剩余业务步骤，仍尝试清理，退出码为 1。清理会先请求取消已知的未完成任务，再软删除租户并吊销数据库令牌。若删除接口返回 404、405 或 501，改为暂停该租户，签发的令牌有效期为一天。软删除保留历史任务和用量。启用 Stripe 时会创建 Checkout 会话，也可能创建 Stripe 客户记录。验收请使用测试部署和 Stripe 测试模式。

`--stripe-sim` 要求结账链接指向指定的模拟器，使用默认的三次失败暂停阈值。模拟订阅在租户清理时取消。
未配置支付时，Stripe 步骤标记 `SKIP`。配置方法见 [Stripe 模拟器](operations.md#stripe-模拟器)。

- `stripe-checkout` 获取结账链接。
- `stripe-complete` 完成模拟结账，确认套餐从 `free` 切换为 `team` 且订阅激活。
- `stripe-payment-failed` 模拟同一发票连续三次付款失败，确认第三次导致租户暂停。
- `stripe-suspended-submit` 验证暂停期间提交返回 `403 tenant_suspended`。
- `stripe-payment-succeeded` 模拟发票付清，确认租户恢复且租户账单列出已付款发票。

运营人员也可用租户管理员令牌单独检查套餐或导出用量。

```bash
curl -fsS "$SCA_API_URL/v1/portal/tenant-quota" \
  -H "Authorization: Bearer $SCA_TENANT_TOKEN"
curl -fsS "$SCA_API_URL/v1/usage/export.csv" \
  -H "Authorization: Bearer $SCA_TENANT_TOKEN" -o usage.csv
```

## 功能清单

当前状态依据已有测试与现场记录。烟测步骤以脚本输出的 ID 标示。

| 验收项 | 客户或运营人员可以完成的操作 | API 或 Portal 入口 | 验证依据 | 当前状态 |
| --- | --- | --- | --- | --- |
| 无 OIDC 开户 | 运营人员创建 `team` 租户并签发首个管理员令牌。管理员随即可访问自己的租户和配额。 | `POST /v1/admin/tenants`、`POST /v1/admin/tenants/{id}/tokens`、`GET /v1/tenant`。Portal「平台管理」可创建租户，首令牌通过 API 签发。 | `tenant-create`、`tenant-bootstrap`、`tenant-quota`。`test_operator_bootstraps_a_tenant_admin_token_without_oidc`。[开户操作](operations.md#新建租户和邀请成员) | 已实现，有租户集成用例和烟测步骤。需要目标部署的平台管理员令牌。 |
| 成员、角色与令牌 | 管理员可邀请、改角色、移除成员和签发、列出、吊销租户令牌。成员可提交，只读角色可查看任务，提交返回 `403 forbidden`。 | `/v1/tenant/members`、`/v1/tenant/tokens`，Portal「团队与用量」。 | `members`、`token-roles`。`test_tenant_admin_changes_roles_and_removes_members_but_not_owners`、`test_tenant_tokens_are_scoped_and_revocable`。[租户集成用例](../tests/integration/test_tenancy_billing.py) | 已实现。烟测检查成员变更、令牌列表和只读角色拒绝提交。用 OIDC 领取邀请需客户 IdP 和已验证的邮箱。 |
| 编码、验证与审核 | 成员提交仓库、需求和验证命令。审核人下载通过验证的补丁、查看影响并批准或要求修改。批准后任务进入 `SUCCEEDED`。 | `POST /v1/tasks`、`GET /v1/tasks/{id}`、`GET /v1/tasks/{id}/artifacts/{name}`、`POST /v1/tasks/{id}/review`，Portal「提交任务」「任务进度」。 | `coding`、`coding-review`。`test_codex_fixes_bug_then_applies_review_feedback`。[Codex 端到端用例](../tests/e2e/test_e2e_codex.py) | 有真实 Codex 回归记录。目标部署需要可用的 Agent、模型凭证和测试仓库。若任务已先进入 `SUCCEEDED`，烟测仍检查补丁和影响，审批步骤标记 `SKIP`。 |
| GitHub 与 Gitea PR | Agent 推送变更并创建 PR。审核人员可刷新 PR 状态、要求修改、批准或拒绝。SCA 更新原 PR 并回写结果，配置后可尝试合并已审核提交。 | `POST /v1/tasks` 的 `spec.push`、`spec.pull_request`，`GET /v1/tasks/{id}/pr` 和 `POST /v1/tasks/{id}/review`，Portal「任务进度 → 拉取请求与工单」。 | 2026-11-21 现场记录及 `test_fix_turn_updates_pull_request_and_merges_reviewed_commit`。[SCM 现场记录](scm.md#现场验证2026-11-21) · [工作流用例](../tests/unit/test_scm_workflow.py) | 已现场验证 GitHub 和 Gitea 建 PR 与批准留言。请求修改和合并有模拟托管方用例。客户需提供测试仓库及 OpenBao 中的 GitHub App 安装凭证或 Gitea 写入令牌。 |
| 工单触发 | 运营人员配置仓库和触发标签。匹配标签或 `/sca run` 评论自动建任务，任务结果和 PR 链接写回工单。 | `/v1/integrations/scm`、`POST /v1/webhooks/github`、`POST /v1/webhooks/gitea`，Portal「集成」。 | 2026-11-21 Gitea 现场记录。GitHub 与 Gitea 的 `test_webhook_builds_task_with_defaults_and_deduplicates_label_delivery`、`test_comment_command_triggers_with_additional_instruction`。[配置与记录](scm.md) · [Webhook 用例](../tests/unit/test_scm_webhooks.py) | Gitea 已实测标签、评论和回写。GitHub 工单触发有模拟请求用例。现场复验需要客户仓库权限及独立的 webhook 签名密钥。 |
| Best-of-N | 成员从同一基线发起多个独立尝试。审核人比较验证、费用和补丁，可自动选取最低费用的通过项或手选候选，再审核选中的补丁。 | `POST /v1/tasks` 的 `spec.best_of`，`GET /v1/tasks/{id}/best-of`、`POST /v1/tasks/{id}/best-of/select`，Portal「提交任务」「Best-of-N 尝试」。 | `test_best_of_workflow` 的自动和人工选择两种策略。[Best-of-N 使用与验证](design-phase4.md#第六期编码任务-best-of-n) · [工作流用例](../tests/integration/test_best_of_workflow.py) | 本地 Git 与假沙箱有分叉、预算和选择的验证记录。需在目标 Worker 上用实际模型和仓库复验。 |
| 子任务与影响 | 成员手动拆分或自动规划任务。依赖通过后才启动后续子任务，修改受路径范围约束。审核人可查询已发布补丁的变更文件、受影响文件和建议测试。 | `POST /v1/tasks` 的 `spec.subtasks`、`spec.plan`，`GET /v1/tasks/{id}/impact`，Portal「提交任务」「代码影响分析」。 | `coding` 检查烟测补丁的变更路径。`test_dag_starts_dependencies_as_soon_as_ready`、`test_scope_rejects_turn_and_keeps_session_for_retry`、`test_impact_endpoint_uses_published_commit_and_durable_bundle`。[子任务使用与验证](decouple.md) | 有本地 Git 和脚本化 Agent 的验证记录。目标仓库的拆分、合并和影响提示需复验。 |
| 工具和知识库 | 管理员登记 MCP 工具、按租户、团队或用户授权，并上传知识文档。成员可检索获准的文档及引用，经授权的 OpenCode 任务可调用知识库和远程工具。 | `/v1/tools`、`/v1/tools/{name}/grants`、`/v1/knowledge/documents`、`POST /v1/knowledge/search`、`/v1/mcp/knowledge`，Portal「工具与知识库」。 | `knowledge`。`test_knowledge_http_acl_and_chunk_cascade`、`test_registry_crud_and_user_team_tenant_grants`、`test_mcp_initialize_list_and_call_respects_team_and_tenant`。[授权与知识库使用](tools.md) | 烟测覆盖管理员上传及成员检索。MCP 有模拟调用用例。远程工具需客户 MCP 地址和相应凭证，向量检索另需部署所选的嵌入模型。 |
| 套餐、暂停与恢复 | 运营人员可设置套餐、查看并发与费用上限、暂停和恢复提交。超出金额上限返回 `402 quota_exceeded`，暂停时成员提交返回 `403 tenant_suspended`。 | `/v1/admin/plans`、`PATCH /v1/admin/tenants/{id}`、`POST /v1/admin/tenants/{id}/suspend`、`POST /v1/admin/tenants/{id}/resume`，Portal「平台管理」「团队与用量」。 | `tenant-quota`、`suspend`、`suspended-submit`、`resume`、`cleanup`。`test_plan_limits_and_suspension`。[套餐与用量](operations.md#套餐与用量) | 已实现。烟测读取 `team` 配额并核对拒绝和恢复，金额与并发限制有集成用例。 |
| 用量计量及 CSV | 租户核对已完成任务、轮次、Token、模型费用及沙箱时间，运营人员可查看按租户汇总的用量。租户可导出带月份、套餐、使用量及账单金额的 CSV。 | `GET /v1/usage`、`GET /v1/admin/usage`、`GET /v1/usage/export.csv`，Portal「团队与用量」「平台管理」。 | `usage`、`usage-csv`。`test_terminal_metering_and_exports`。[用量集成用例](../tests/integration/test_tenancy_billing.py) | 集成用例覆盖汇总和跨租户视图。烟测在独立租户校验任务计数及 CSV，默认模式验证零用量，启用 Agent 时验证一项终态任务。真实模型金额需要配置网关。 |
| Stripe 结账、回调与发票 | 租户选择套餐并结账，可管理付款方式、查看订阅和发票。运营人员可核对签名 webhook、欠费暂停、付款恢复及月度超额账单。 | `/v1/tenant/billing`、`POST /v1/tenant/billing/checkout`、`POST /v1/tenant/billing/portal`、`POST /v1/billing/stripe/webhook`、`GET /v1/admin/billing`，Portal「团队与用量 → 订阅与付款」。 | `--stripe-sim` 烟测覆盖结账、暂停、恢复和发票。`test_checkout_webhook_plan_changes_and_recovery`、`test_monthly_overage_is_reported_once_after_a_lost_stripe_response`。[Stripe 模拟器](operations.md#stripe-模拟器) · [付款集成用例](../tests/integration/test_stripe_payments.py) | **模拟器端到端验证；真实 Stripe 需客户凭证**。真实测试模式需要测试密钥、签名密钥和 `team` Price。 |
| 自动演进 | 管理员发起或定期运行 OpenCode 演进，查看开发集与留出集证据、批准或拒绝晋升，查阅候选和冠军谱系。 | `/v1/evolutions`、`/v1/evolution-schedules`，Portal「自动演进」。 | `test_evolution_paths`、`test_scheduler_evaluates_current_champion_and_records_outcome`。[演进操作](design-evolution.md#6-产品化) · [工作流用例](../tests/integration/test_evolution_workflow.py) · [长程套件现场校准](../evals/opencode-evolve-longhaul/README.md) | 有真实模型的开发集校准记录。晋升、拒绝和调度有模拟评测用例。目标部署需用评测令牌、支持覆盖的 OpenCode 版本、模型凭证及含 dev、holdout 的套件复验。 |
| 冠军灰度与回滚 | 租户管理员按版本和模型调整灰度，任务显示实际选用的冠军。`eval` 管理员可回滚至父冠军，后续任务按回滚后的配置选择。 | `GET/PUT /v1/champions/rollouts`、`POST /v1/champions/{id}/rollback`，Portal「自动演进」「任务进度」。 | `test_tenant_and_platform_rollout`、`test_rollout_rollback_updates_tenants_and_audit_but_keeps_task_pin`。[灰度与回滚操作](design-evolution.md#61-任务使用冠军与灰度回滚) · [数据库用例](../tests/integration/test_evolution_product_store.py) | 有分桶、回滚审计和任务固定版本的测试。目标部署需有含父版本的冠军谱系，复验回滚前后新任务的选用记录。固化和配对回归有模拟构建和评测测试。[固化使用与验证](design-evolution.md#固化与默认版本) |
| 设计稿和工单上下文 | 成员把 Figma、设计 Token 或外部工单加入编码任务，查看生成的检查和终态回写结果。 | `POST /v1/files`、`POST /v1/tasks` 的 `spec.intents`，Portal「提交任务」。 | `test_figma_compile_through_kernel`、`test_jira_read_and_write_outcome`、`test_linear_paginated_ticket_attachments_and_graphql_writeback`。[配置与验证](intents.md) | 远程服务采用模拟 API 验证。现场需客户提供相应服务凭证和测试数据。 |
| 实时终端 | 任务查看者可在支持的 OpenCode 脑手分离部署查看沙箱输出。 | `GET /v1/tasks/{id}/terminal`，Portal「任务进度 → 观看输出」。 | `test_tenant_viewer_terminal_sse_and_live_input`、`test_brain_tool_transport_and_latency`。[终端与传输用例](../tests/unit/test_brain_handd.py) | 有终端 SSE 与 PTY 传输测试。需在目标 `opencode-brain` 和 `SCA_SANDBOX_HANDD=true` 的部署复验。 |
| Portal | 平台管理员能创建、管理租户。租户管理员能管理团队、配额、用量、账单、工具和集成。成员能提交、审阅、查看候选和影响，只读角色可查看获准的任务。 | `/portal` 的「平台管理」「团队与用量」「工具与知识库」「集成」「提交任务」「任务进度」「自动演进」。 | `test_phase6_portal_navigation_and_local_assets`、`test_portal_phase6_browser_submission`、`test_portal_quota_with_real_admission_and_usage`。[界面用例](../tests/unit/test_portal_phase6_static.py) · [Portal 现场记录](../Milestone/portal-run-2026-11-19.md) | 有导航、模拟 API 的表单和权限用例，以及早期评测 Portal 的现场记录。烟测通过 HTTP 验证业务接口，新增页面需用目标部署的各角色在浏览器中验收。 |

## 现场烟测记录（2026-11-21）

`scripts/commercial_smoke.py --api http://127.0.0.1:8000`（单机演示部署，提交 9964fd0 之后）：12 项 PASS、0 FAIL、3 项 SKIP。
SKIP：`coding`、`coding-review`（DeepSeek 账户余额不足，模型调用返回 402，待充值后用 `--with-agent --repo` 补跑；
同类流程此前已现场通过，见 docs/scm.md）；`stripe-checkout`（未配置 Stripe 测试密钥，假 Stripe 现场验证见 docs/operations.md）。
