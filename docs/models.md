# 模型接入

## 在 Portal 添加模型

通过 Portal 启动脚本启动平台，用本机 `eval` 管理员令牌登录。打开「概览 → 模型接入」，填写供应商名称、类型和基础 URL。需要认证的服务可填写 API Key。免密的本地服务可留空。

每个供应商至少添加一个模型。「平台模型名」是任务、项目和评测中选择的名称。「上游模型 ID」填写服务实际提供的 ID，比如 `org/coder` 或 `qwen2.5-coder:7b`。可为每个模型填写上下文窗口和输入、输出价格。保存或删除后，Portal 显示网关重启进度。Docker 模式同时重启正在运行的 Worker。重启成功后，使用「测试连接」让网关发出一次短补全并显示耗时或错误。若重启失败，已保存的配置仍可查看。检查网关状态后执行下方的重启命令。

在「提交任务」「评测中心」「自动演进」或「项目 → 设置」选择平台模型名。「自动演进」还可为反思和候选生成选择独立的 `evolve_model`。省略时优先使用 `SCA_EVOLVE_MODEL`，本机仅配置其他模型时会选择可用模型。任务可使用项目默认模型或平台默认模型。

内置 DeepSeek 使用上方的 DeepSeek API Key 表单。已有 `.env` 中的 `DEEPSEEK_API_KEY` 会继续启用 `deepseek-flash` 和 `deepseek-v4-pro`。自定义供应商可选择 `deepseek`、`openai-compatible` 或 `anthropic-compatible`。内置模型名由平台保留。

### Docker 升级后启用模型接入

在已安装的仓库运行 `bash scripts/portal_demo.sh up --mode docker`，启动脚本会从仓库路径补齐旧配置缺失的 `SCA_PORTAL_LOCAL_CONFIG`。升级前 API 与 Worker 已在运行时，先执行 `bash scripts/portal_demo.sh down` 再启动。登录令牌仍保存在 `.data/portal.env` 的 `SCA_EVAL_API_TOKEN`。供应商接口的 `403` 会区分租户、客户端地址和本机配置，按提示切换到 `eval` 管理员令牌、在 Portal 所在机器访问或通过启动脚本重新启动。

## 基础 URL 与运行模式

| 服务 | 类型 | 基础 URL 示例 |
| --- | --- | --- |
| vLLM | `openai-compatible` | `http://127.0.0.1:8001/v1` |
| Ollama | `openai-compatible` | `http://<host>:11434/v1` |
| LM Studio | `openai-compatible` | `http://127.0.0.1:1234/v1` |
| Anthropic 兼容网关 | `anthropic-compatible` | `https://models.example.com`（LiteLLM 添加 `/v1/messages`） |

- **Windows、Linux 或 macOS 源码模式**。LiteLLM 在宿主机上运行，可用 `127.0.0.1` 访问本机服务。另一台机器上的服务使用网关能访问的主机名或 IP。
- **Docker 模式**。LiteLLM 将回环地址转换为 `host.docker.internal`。Linux 通过 Compose 的 `host-gateway` 映射解析该地址。模型服务必须监听 Docker 网桥可到达的宿主机地址，且防火墙需允许该连接。Docker Desktop 提供 `host.docker.internal`。使用 HTTPS 时，证书主机名需与网关实际使用的地址匹配。沙箱继续用网关地址与短期密钥。 [Docker Compose 网络](https://docs.docker.com/compose/how-tos/networking/)

注册模型的 OpenCode 和 Pi 请求使用 Chat Completions。DSH 直连 LiteLLM 时可发送 Messages，由 LiteLLM 为 OpenAI 兼容上游转换成 Chat Completions。启用 SCA 限流代理时，DSH 默认发送 Chat Completions。Codex 使用 Responses，由 LiteLLM 为 Chat Completions 上游转换。模型及其服务必须支持相应的流式响应和工具调用，vLLM 需要启用与所用模型匹配的工具解析器。短补全测试验证基本连通性。 [LiteLLM 协议转换](https://docs.litellm.ai/docs/supported_endpoints) [Responses 转换](https://docs.litellm.ai/docs/response_api_native_passthrough)

自定义凭证保存在 `SCA_DATA_DIR` 下的本机密钥文件 `model-providers.json`，默认目录是 `.data`。LiteLLM 从环境变量取密钥，生成的 `litellm.generated.yaml` 只包含引用。对接 API 中的密钥字段只写。手动重启请使用对应的启动脚本。

```bash
bash scripts/portal_demo.sh restart-gateway
```

```powershell
./scripts/portal_source.ps1 restart-gateway
```

## 价格与预算

每项价格单位为美元 / 百万 Token。单项留空视为该项费率 0。输入和输出费率都为 0 的模型按 $0 计费，任务的短期密钥不设美元上限。任务页面可填写 `max_tokens`，平台在轮次之间检查累计 Token。未定价的评测用例不占用评测美元预算，演进对未定价的评测模型同样不执行该美元限制。表单选择未定价模型时会显示提示。反思和候选生成请求沿用网关配置，演进的美元预算用于用例评测。

未定价模型的演进以实测总 Token 衡量效率，质量及配对要求继续适用。评测模型需要报告用量，平台会按原有规则比较付费模型的美元支出。请在运行完成后再改动正在使用的模型或价格。

## API 示例

在本机通过已经设置的 `$SCA_API_URL` 和 `$SCA_API_TOKEN` 调用。供应商管理使用本机 `eval` 管理员令牌。已登录的租户用户可通过 `GET /v1/models` 查看当前已加载的模型名、上下文窗口和计费状态。

```bash
curl -fsS "$SCA_API_URL/v1/portal/providers" \
  -H "Authorization: Bearer $SCA_API_TOKEN" --json @- <<'JSON'
{
  "name": "本机 Ollama",
  "type": "openai-compatible",
  "base_url": "http://127.0.0.1:11434/v1",
  "models": [{"name": "local-coder", "upstream_model": "qwen2.5-coder:7b", "context_window": 8192}]
}
JSON
```

`POST`、`PUT` 和 `DELETE` 保存配置后立即返回 HTTP `202`，响应包含 `restart.id` 和 `restart.status`，初始状态为 `restarting`。`POST` 和 `PUT` 还返回 `provider`。将 `RESTART_ID` 设置为 `restart.id`，轮询以下接口直至状态变为 `succeeded` 或 `failed`。当前 API 进程保留最近 32 条重启记录，重启 API 后可检查网关状态并按需手动重启网关。重启进行中新增、编辑、删除或测试连接会收到 HTTP `409`，待完成后再试。

```bash
curl -fsS "$SCA_API_URL/v1/portal/providers/restarts/$RESTART_ID" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
```

状态为 `succeeded` 后，测试连接并检查模型列表。

```bash
curl -fsS "$SCA_API_URL/v1/portal/providers/test" \
  -H "Authorization: Bearer $SCA_API_TOKEN" --json '{"model":"local-coder"}'

curl -fsS "$SCA_API_URL/v1/models" -H "Authorization: Bearer $SCA_API_TOKEN"
```

`GET /v1/portal/providers` 列出已保存供应商和启用的内置 DeepSeek 模型。使用创建结果的 `provider.id` 调用 `PUT /v1/portal/providers/{id}`，提交完整的供应商配置。省略 `api_key` 保留现有密钥，传 `null` 清除，传字符串替换。修改基础 URL 或接口类型时，已有密钥需重新填写或清除。使用 `DELETE /v1/portal/providers/{id}` 删除供应商。内置 DeepSeek 密钥通过 `POST /v1/portal/provider-key` 保存，仍在响应中用 `gateway_restarted` 报告是否重启成功。

```bash
curl -fsS "$SCA_API_URL/v1/evolutions" \
  -H "Authorization: Bearer $SCA_API_TOKEN" \
  --json '{"suite":"dsh-smoke","agent_backend":"dsh","model":"local-coder","evolve_model":"local-coder"}'
```
