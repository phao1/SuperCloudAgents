# GitHub 与 Gitea 集成

## 配置仓库凭证

先将数据库迁移到版本 `0018`。API 与 Worker 均使用 `SCA_SECRET_PROVIDER=openbao`，沿用现有的 OpenBao KV v2 仓库路径 `sca/tenants/<租户>/repos/<主机名>/<所有者>/<仓库>`。路径中的主机名省略端口，并保持仓库克隆 URL 中的路径大小写。Worker 用凭证推送分支及调用仓库 API，API 用同一租户的凭证刷新 PR。参见 [仓库凭证约定](design-phase2b.md#b43-用-openbao)。

- **GitHub** 安装 GitHub App 到目标仓库，将 `type=github_app`、`app_id`、`installation_id`、`private_key` 写入 OpenBao。为 App 配置 `Contents: Read and write`、`Pull requests: Read and write`（`pull_requests: write`）和 `Issues: Read and write`（`issues: write`）。仓库克隆 URL 使用 `https://github.com/OWNER/REPO.git`。 [GitHub App 权限](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)
- **Gitea** 为有仓库写权限的账号生成访问令牌，授予 `write:repository` 和 `write:issue`。将 `type=token`、`username`、`token` 写入 OpenBao。通过 `SCA_SCM_GITEA_HOSTS` 将克隆 URL 的主机和端口映射到 API v1 地址。 [Gitea 令牌权限](https://docs.gitea.com/development/oauth2-provider/)

API 和 Worker 使用同一套仓库主机配置。将 `SCA_SCM_PORTAL_URL` 指向用户能访问的 Portal，默认从 `SCA_API_URL` 加上 `/portal` 取得。测试可使用 `SCA_SCM_GITHUB_API_URL` 覆盖 GitHub REST 根地址。

```bash
export SCA_SCM_GITEA_HOSTS='{"forge.example.com":"https://forge.example.com/api/v1"}'
export SCA_SCM_PORTAL_URL='https://sca.example.com/portal'
```

## 通过编码任务创建 PR

传入 `push: true` 和 `pull_request`。PR 默认在任务推送分支并进入 `AWAITING_REVIEW` 后创建。`open_on: "approve"` 将创建延后到人工批准。`base` 默认采用任务的 `base_ref`，`title` 和 `body` 可选，SCA 会附上任务指令、验证结果、模型费用和 Portal 链接。再次提交修改后会推送同一分支并更新 PR 中的 SCA 验证摘要。

```bash
curl -fsS "$SCA_URL/v1/tasks" -H "Authorization: Bearer $SCA_TOKEN" --json @- <<'JSON'
{
  "kind": "coding",
  "spec": {
    "repo_url": "https://github.com/acme/checkout.git",
    "instruction": "修复空购物车的结算提示，并补回归测试",
    "verify_command": "pytest -q",
    "push": true,
    "pull_request": {"title": "修复空购物车结算", "open_on": "review", "merge_on_approve": true}
  }
}
JSON
```

`task.result.pr` 与 `GET /v1/tasks/{id}/pr` 提供 `provider`、`number`、`url`、`state`，后者从托管平台刷新。创建成功会写入 `pr.opened` 事件。人工通过或拒绝时，SCA 在已有 PR 上留言。使用 `merge_on_approve: true` 时，批准会尝试合并已审核的提交，并由托管平台执行分支保护规则。要使用自动合并，请确保批准时 PR 已可合并。对等待 SCA 审核的草稿 PR，请先在托管平台标为可审阅。Gitea 1.22 的草稿使用默认 `WIP:` 标题前缀。托管平台返回的操作失败记在任务的 `scm.failed` 事件中。

```bash
curl -fsS "$SCA_URL/v1/tasks/$TASK_ID/pr" -H "Authorization: Bearer $SCA_TOKEN"
curl -fsS "$SCA_URL/v1/tasks/$TASK_ID/review" -H "Authorization: Bearer $SCA_TOKEN" \
  --json '{"decision":"approve"}'
```

## 通过工单触发任务

1. 在 API 的运行环境配置 `SCA_SCM_WEBHOOK_SECRETS`，其 JSON 结构为 `{"租户":{"密钥引用":"从密钥管理器注入的值"}}`。每个集成使用独立的随机密钥，数据库只保存引用。
2. 用租户管理员令牌注册仓库。`trigger_labels` 可以包含一个或多个标签。出现匹配标签的工单可自动创建一次任务，配置空列表时新建工单会触发。工单中的每条 `/sca run` 评论可独立启动任务，命令后的文字附加到工单标题和正文之后。
3. 在仓库设置添加 JSON webhook。GitHub 使用 `POST /v1/webhooks/github` 并订阅 **Issues**、**Issue comments**。Gitea 使用 `POST /v1/webhooks/gitea` 并订阅工单、工单标签和工单评论事件。两者均配置步骤 1 中为该集成生成的密钥。 [GitHub 验签](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) · [Gitea webhook](https://docs.gitea.com/usage/repository/webhooks/)

```bash
curl -fsS "$SCA_URL/v1/integrations/scm" -H "Authorization: Bearer $SCA_TOKEN" --json @- <<'JSON'
{
  "provider": "github",
  "host": "github.com",
  "repo_full_name": "acme/checkout",
  "webhook_secret_ref": "checkout-hook",
  "default_spec": {
    "verify_command": "pytest -q",
    "setup_command": "pip install -e .",
    "trigger_labels": ["sca"],
    "pull_request": {"open_on": "review"}
  },
  "enabled": true
}
JSON
```

管理员可用 `GET /v1/integrations/scm` 列出集成，并在 `/v1/integrations/scm/{id}` 使用 `GET` 查看、`PUT` 替换整条配置、`DELETE` 删除。将 `enabled` 设为 `false` 可暂停触发。工单任务使用仓库默认分支，也可通过 `default_spec.base_ref` 覆盖。`default_spec` 还支持 `verify_commands` 和 `image`。

成功触发后，webhook 返回 HTTP 202 和 `task_id`。任务保存 `task.result.issue`。结束时，SCA 在原工单留言报告结果，已生成 PR 时附上链接。
