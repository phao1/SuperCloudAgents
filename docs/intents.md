# 设计稿、Design Tokens 与工单

编码任务可在 `POST /v1/tasks` 的 `spec.intents` 中组合设计稿、Token 和工单。Worker 在执行前读取来源，将说明和文件放入任务的 `.sca/context/`，并把对应的检查加入任务验证。各来源的 `name` 在同一任务中必须唯一。

## 配置 Worker 凭证

运维通过部署密钥注入环境变量 `SCA_INTENT_CREDENTIALS`。下例展示 JSON 结构。外层键为 SCA 租户，内层键为「来源类型/密钥名称」。

```json
{
  "acme": {
    "figma/design": {"token": "<Figma 个人访问令牌>"},
    "jira/tracker": {
      "token": "<Jira Cloud API 令牌>",
      "email": "automation@example.com",
      "site": "https://acme.atlassian.net"
    },
    "linear/tracker": {"token": "<Linear 个人 API 密钥>"},
    "github_issue/acme/website": {"token": "<GitHub 访问令牌>"}
  }
}
```

- Figma 需要 `file_content:read`。读取本地变量还需要 `file_variables:read` 和相应的 Figma 企业版权限。Worker 默认访问 `https://api.figma.com`，测试可设置 `SCA_FIGMA_API_URL`。[Figma 文件接口](https://developers.figma.com/docs/rest-api/file-endpoints/) · [变量接口](https://developers.figma.com/docs/rest-api/variables-endpoints/)
- Jira Cloud 使用绑定到该站点的账号邮箱和 API 令牌。回写权限取决于工单所在项目的配置。[Jira Cloud REST API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/)
- Linear 密钥需有读取、发布评论和所需的状态变更权限。[Linear GraphQL API](https://linear.app/developers/graphql)
- GitHub 优先使用上例中以小写 `owner/repo` 配置的令牌，也可以复用该租户的 OpenBao 仓库凭证。公共工单可匿名读取。写回需要仓库的 Issues 写权限。[GitHub Issues API](https://docs.github.com/en/rest/issues/issues) · [评论接口](https://docs.github.com/en/rest/issues/comments)

任务 JSON 只填写 `token_secret` 名称。凭证由 Worker 查找，Jira 请求的站点必须与该凭证中配置的站点一致。

## 上传 Token 并提交任务

```bash
curl -fsS "$SCA_URL/v1/files" \
  -H "Authorization: Bearer $SCA_TOKEN" \
  -F "file=@tokens.json"
```

将上传响应中的 `id` 填入下例的 `file_id`。把示例保存为 `task.json`，替换仓库、工单和设计稿标识，再提交。

```json
{
  "kind": "coding",
  "spec": {
    "repo_url": "https://github.com/acme/website.git",
    "instruction": "按设计稿和工单验收标准实现结账页",
    "image": "sca-sandbox-web:latest",
    "setup_command": "npm ci",
    "verify_command": "npm test",
    "intents": [
      {
        "type": "figma",
        "name": "checkout-design",
        "params": {
          "file_key": "FigmaFileKey",
          "node_ids": ["12:34"],
          "token_secret": "design",
          "visual": {
            "12:34": {
              "start_command": "npm run dev -- --host 0.0.0.0",
              "base_url": "http://127.0.0.1:5173",
              "page_url": "http://127.0.0.1:5173/checkout"
            }
          }
        }
      },
      {
        "type": "design_tokens",
        "name": "site-tokens",
        "file_id": "<上传响应中的 id>",
        "params": {"css_variables": true}
      },
      {
        "type": "jira",
        "name": "jira-checkout",
        "params": {
          "site": "https://acme.atlassian.net",
          "issue_key": "WEB-123",
          "token_secret": "tracker",
          "acceptance_field": "customfield_10037",
          "transition_id": "31"
        }
      },
      {
        "type": "linear",
        "name": "linear-checkout",
        "params": {"issue_id": "WEB-124", "token_secret": "tracker", "state_id": "<Linear 状态 ID>"}
      },
      {
        "type": "github_issue",
        "name": "github-checkout",
        "params": {"repo": "acme/website", "number": 125}
      }
    ]
  }
}
```

```bash
curl -fsS "$SCA_URL/v1/tasks" \
  -H "Authorization: Bearer $SCA_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @task.json
```

## 任务运行和结果

- **Figma**。指定 `node_ids` 时读取这些节点。省略时读取最多八个可见的顶层 Frame 或组件。Worker 下载 PNG，并记录文字、布局、颜色、字体与组件实例信息。为节点配置 `visual` 可启用 Playwright 截图对比，视口默认取 PNG 尺寸，可用 `width` 和 `height` 覆盖。仅用参考图时可省略 `visual`。可访问的变量按集合默认模式导出为 `<name>-tokens.json`。可通过 `variable_modes` 以集合 ID 指定模式 ID。颜色、带尺寸作用域的数字、普通数字和字体变量可转换为 DTCG。
- **Design Tokens**。支持 DTCG 的颜色、尺寸、字体、字重和排版组合，以及 `{group.token}` 别名。引用不存在或成环时，意图编译会失败。也支持 Tokens Studio 单文件导出的常用类型。有多个 Token Set 时，默认按 `$metadata.tokenSetOrder` 合并，可用 `params.token_sets` 指定顺序和范围。检查对比任务 Git 基线与当前提交，在变更的 CSS、SCSS、TSX 文件中提示与 Token 匹配的硬编码颜色和尺寸。打开 `css_variables` 后会在上下文生成供参考的 CSS 文件，项目可选择引入。[DTCG 格式规范](https://www.designtokens.org/TR/2025.10/format/) · [Tokens Studio 格式说明](https://docs.tokens.studio/manage-settings/token-format)
- **工单**。任务上下文包含标题、说明、验收标准、评论与附件。Jira 可用 `acceptance_field` 指定自定义验收字段。Linear 和 GitHub 可在说明中用 `Acceptance criteria` 或「验收标准」标题组织内容。每个工单最多读四页评论并下载前十二个附件，单个上下文文件上限为 2 MiB。受支持的服务托管图片保存为视觉参考。外部链接和下载失败的附件保留在工单说明中。
- **回写**。编码任务进入 `SUCCEEDED`、`FAILED` 或 `CANCELLED` 后，Worker 为每个工单发布状态评论。如果任务结果已有 PR 链接，评论也包含该链接。Jira 的 `transition_id` 和 Linear 的 `state_id` 仅在成功后应用。工单来源可用 `params.writeback: false` 只读导入。回写完成或失败可通过 `GET /v1/tasks/{task_id}` 的 `result.ticket_writeback` 和 `GET /v1/tasks/{task_id}/events` 中的 `intents.ticket_writeback` 查询。

## 验证

意图相关单元测试 44 项通过，覆盖四个服务的 HTTP 模拟、终态回写及实际 Git 变更检查。可在当前沙箱运行的单元回归有 548 项通过、4 项跳过。无需数据库的工作流上下文集成测试有 12 项通过。当前沙箱禁止创建 PostgreSQL 所需的套接字。真实服务和 Playwright 视觉对比尚待部署环境验证。
