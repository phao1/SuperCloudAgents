# 项目与线程

项目保存一段持续工作的目标、仓库、默认后端与模型，以及新线程共用的指令。项目内的线程独立执行和审核，编码线程可推送任务分支并创建拉取请求。

| SCA 概念 | Claude Projects 相近概念 | 在 SCA 中的含义 |
| --- | --- | --- |
| 项目 | Project | 租户下有名称的工作空间。首页显示线程状态数量和最近活动时间。 |
| 线程 | Thread | 项目中的顶层任务。沿用任务 ID、独立会话、沙箱、产物和审核流程。 |
| 子任务 | 线程内划分的工作 | 编码规划产生的子任务和 Best-of-N 尝试由父线程承载，事件、产物和用量归入父线程。 |

[Claude Code Projects 概念参考](https://code.claude.com/docs/en/claude-projects)

## 在 Portal 使用

登录 `/portal` 后打开「项目」。选择「新建项目」，填写名称即可创建。可同时填写一行目标、仓库及基线、项目指令、默认后端和模型。首个仓库为默认仓库，项目指令最多 16,000 字符。

进入项目后选择「新线程」填写需求。可用现有表单配置验证命令、推送与 PR、任务拆分以及已授权的工具。「所有线程」仍可访问原来的任务列表和详情，详情页可经面包屑返回项目。

| 线程分组 | 显示条件 |
| --- | --- |
| 待审核 | 等待对发布结果做人工审核。 |
| 等你处理 | 暂停、等待输入、待确认计划或待选 Best-of-N 候选。 |
| 进行中 | 排队、准备、执行或验证。 |
| 合入中 | 等待外部 CI 或 PR 处于打开、合入状态。待审的 PR 仍显示在「待审核」。 |
| 空闲 | 暂无待处理工作且处于其他非终态。 |
| 已完成 | 成功、失败或取消。 |

「设置」修改后只影响新线程。成员和管理员可创建、编辑、暂停和归档项目。查看者可浏览线程、文件、用量和设置。管理员可上传知识文档及删除项目。

暂停或归档项目后，提交新线程返回 `409`。运行中的线程在当前执行环节到达安全检查点后暂停。外部 CI 可继续执行并回报结果。恢复或取消归档将继续因项目暂停而等待的线程，单独暂停的线程需单独恢复。归档项目仍可从项目列表打开。管理员可在项目没有进行中的线程时软删除项目，结束仍在等待的线程，并保留历史任务及其产物。

## API 示例

以下示例使用已有租户令牌。创建后，将返回的项目 ID 写入 `PROJECT_ID`。

```bash
curl "$SCA_URL/v1/projects" \
  -H "Authorization: Bearer $SCA_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"结算服务","goal":"补齐账单回归覆盖","instructions":"提交前运行结算回归测试。","default_backend":"codex","repos":[{"repo_url":"https://git.example.org/team/billing.git","base_ref":"main"}]}'

curl "$SCA_URL/v1/tasks" \
  -H "Authorization: Bearer $SCA_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJECT_ID\",\"kind\":\"coding\",\"spec\":{\"instruction\":\"补齐退款回归用例\",\"verify_command\":\"pytest -q\"}}"

curl "$SCA_URL/v1/projects/$PROJECT_ID/threads?group=ready_for_review" \
  -H "Authorization: Bearer $SCA_TOKEN"

curl -X POST "$SCA_URL/v1/projects/$PROJECT_ID/pause" \
  -H "Authorization: Bearer $SCA_TOKEN"
```

`GET /v1/projects` 返回项目卡片数据。`GET /v1/projects/{id}` 返回项目设置，`PATCH` 支持按字段更新，`settings` 可保存 JSON 项目元数据。生命周期接口在同一路径后追加 `/pause`、`/resume`、`/archive` 或 `/unarchive`，软删除使用 `DELETE /v1/projects/{id}`。

提交时省略后端或模型会使用项目默认值。编码任务省略仓库时使用首个仓库，省略基线且仓库已在项目中配置时使用该仓库的基线。提交时保存生效的任务规格，项目指令以 `Project instructions` 小节放在线程需求前。省略 `project_id` 的任务进入租户的「默认项目」，首次提交时自动创建。

### 文件库、记忆和用量

- `GET /v1/projects/{id}/library` 返回可访问的项目及租户共享知识文档，以及该项目线程的产物。文档列表支持 `limit` 和 `offset`。Portal 可查看文档及下载产物。
- `POST /v1/knowledge/documents` 可附带 `project_id`，例如 `{"project_id":"<项目 ID>","title":"结算手册","format":"markdown","text":"# 结算流程"}`。列表、正文和搜索均支持项目范围，既有团队授权仍适用。线程中的知识库 MCP 工具使用该线程的项目范围。
- `POST /v1/memories` 可附带 `project_id`，例如 `{"project_id":"<项目 ID>","scope":"tenant","text":"结算回归使用固定日期。"}`。线程检索匹配租户、场景或仓库的项目记忆及租户共享记忆。由线程提出的记忆沿用线程的项目。可用 `GET /v1/memories?project_id=<项目 ID>` 查看范围内记录。
- `GET /v1/projects/{id}/usage` 返回总计 `totals`、按线程 `by_thread` 和按模型 `by_model` 的轮次、Token、沙箱秒数及美元费用。

## 迁移

部署时运行 `alembic upgrade head`。迁移 `0024_projects` 为已有任务的租户创建一个「默认项目」并关联其顶层任务。已有知识文档及记忆继续作为租户共享资料。
