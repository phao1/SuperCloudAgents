# MCP 工具与企业知识库

运行数据库迁移 `0019_tools` 后，租户管理员可登记远程 MCP 服务，并按租户、团队或用户授权。带 `tools` 的任务使用 `agent_backend: opencode`、模型网关以及受限出口的 Docker 沙箱。远程服务提供 Streamable HTTP MCP 端点，使用 Squid 可访问的 HTTP 80 或 HTTPS 443 域名。带请求头凭证的服务须使用 HTTPS。每个沙箱使用自己的出口规则，规则纳入该任务所选工具的主机名。

## 登记与授权

以下示例使用 API 地址 `$SCA_URL` 和租户 `company` 的管理员令牌 `$ADMIN_TOKEN`。在 OpenBao KV v2 的 `sca/tenants/company/tools/tickets/tickets-auth` 路径下写入字段 `value`，内容是完整请求头值，例如由运维注入的 `Bearer` 凭证。登记时填写引用名称 `tickets-auth`。Worker 沿用 `SCA_SECRET_PROVIDER=openbao`、`SCA_OPENBAO_MOUNT` 和 `SCA_OPENBAO_PREFIX` 的配置读取凭证。

```bash
curl -fsS "$SCA_URL/v1/tools" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name":"tickets", "description":"工单查询", "kind":"remote",
    "url":"https://tickets.example.org/mcp", "headers":{"Authorization":"tickets-auth"}
  }'
curl -fsS "$SCA_URL/v1/tools/tickets/grants" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"subject_type":"team","subject":"ops"}'
curl -fsS "$SCA_URL/v1/tools/available" -H "Authorization: Bearer $USER_TOKEN"
```

团队成员由部署方在 API 和 Worker 进程中统一设置 `SCA_TOOL_TEAMS`，例如 `{"company":{"alice-sub":["ops"]}}`。键 `alice-sub` 是 OIDC 的 `sub`。静态令牌的主体为 `static-token`，数据库令牌的主体为令牌 ID。`user` 授权使用主体作为 `subject`，`tenant` 授权使用租户 ID 作为 `subject`。

管理员可用 `GET /v1/tools` 和 `GET /v1/tools/{name}` 查看配置，`PATCH /v1/tools/{name}` 修改配置或设置 `enabled: false`，`DELETE /v1/tools/{name}` 删除。新工具版本为 `1`，配置更新后递增。`GET /v1/tools/{name}/grants` 查看授权，`DELETE /v1/tools/{name}/grants?subject_type=team&subject=ops` 撤销团队授权。

`platform` 是保留的租户 ID。RLS 允许所有已设置租户上下文的调用读取 `platform` 工具定义。平台注册和共享远程工具由 `platform` 的管理员管理。该管理员创建跨租户授权时在请求体中加上 `tenant_id`，查询或撤销时用同名查询参数。租户管理员可为本租户开启内置 `knowledge` 工具。工具使用需要有效授权，本租户的同名登记优先于共享登记。

## 在任务中使用

```bash
curl -fsS "$SCA_URL/v1/tasks" -H "Authorization: Bearer $USER_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "kind":"coding", "spec":{
      "repo_url":"https://git.example.org/company/app.git",
      "instruction":"查询相关工单并更新测试。",
      "agent_backend":"opencode", "tools":["tickets"]
    }
  }'
```

Worker 按任务提交者检查授权，并在每轮加载工具配置。`tools.attached` 事件记录工具名称和版本。运行中的沙箱沿用创建时的主机列表。Codex、Pi、DeepSeek Harness 和 `opencode-brain` 任务携带 `tools` 时会在启动阶段返回错误。

## 上传和检索知识

管理员通过 JSON 上传正文。`format` 支持 `text`、`markdown` 和 `pdf-text`。PDF 请先提取文本，正文上限为 10 万字符。`allowed_teams` 为空时租户内可读，填写团队后只有匹配的用户能在列表、搜索或 MCP 中读取。Postgres 已启用 pgvector 时可设置 `SCA_MODEL_GATEWAY_URL` 和 `SCA_MEMORY_EMBEDDING_MODEL`，使上传和搜索使用网关的嵌入模型。搜索也支持标题、章节或正文的子串匹配，结果包含 `文档 ID#chunk-分块序号` 格式的引用。

```bash
curl -fsS "$SCA_URL/v1/knowledge/documents" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "title":"运维手册", "format":"markdown",
    "text":"## 密钥轮换\n\n每七天轮换一次。", "allowed_teams":["ops"]
  }'
curl -fsS "$SCA_URL/v1/knowledge/documents" -H "Authorization: Bearer $USER_TOKEN"
curl -fsS "$SCA_URL/v1/knowledge/search" -H "Authorization: Bearer $USER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"query":"轮换","k":5}'
```

管理员用 `DELETE /v1/knowledge/documents/{id}` 删除文档及其索引分块。要让 Agent 检索知识库，在 API 和 Worker 进程中设置同一个 `SCA_KNOWLEDGE_MCP_SIGNING_KEY`（至少 32 字节，通过部署密钥注入）和 `SCA_KNOWLEDGE_MCP_URL`（例如 `https://sca.example.org/v1/mcp/knowledge`）。该 HTTPS 地址须能通过沙箱出口代理访问 API。为任务提交者授权后，在任务规格中加入 `"tools":["knowledge"]`。

```bash
curl -fsS "$SCA_URL/v1/tools/knowledge/grants" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"subject_type":"team","subject":"ops"}'
```

OpenCode 使用 Worker 按轮签发的任务令牌，调用 `/v1/mcp/knowledge` 上的 `search_knowledge(query, k)` 和 `read_document(id)`。API 核验令牌有效期、租户、任务状态、当前授权与文档团队权限。传输采用 [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 验证

单元测试覆盖授权、OpenCode 配置与凭证脱敏、分块、模拟嵌入检索和 ASGI 内的 MCP 调用。本次单元回归中 542 项通过、33 项跳过，`ruff check src tests` 通过。开发沙箱禁止 PostgreSQL 套接字连接，4 项数据库集成测试跳过。迁移已生成离线 SQL，实际 RLS、pgvector 查询及 Docker 中的 OpenCode 联调有待在可用环境中验证。
