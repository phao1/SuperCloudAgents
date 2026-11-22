# SuperCloudAgents

> 🚧 Early stage. It runs end to end on a single machine, with the hardening described under
> [Phase 2](#phase-2) and the commercial basics under [Phase 6](#phase-6-evolution-and-commercial-basics). It is not yet a multi-node production system; see [Limits](#limits).

SuperCloudAgents is an open-source platform for hosting long-running AI agents in the cloud.
You submit a goal; the platform gives the agent an isolated sandbox, lets it work on its own,
checks the result with a command you choose, and only interrupts a human when a decision is
needed.

The kernel is scenario-agnostic. The first scenario is a **coding agent**: give it a repository
and a requirement, get back a branch that passes your tests.

```text
submit (repo + instruction + verify command)
  → clone, create sandbox
  → agent works one turn in the sandbox
  → commit, run the verify command
  → on failure: feed the output back to the agent (bounded retries)
  → publish a patch, optionally push a branch
  → human: approve / request changes / reject
```

## Design

- [`docs/architecture.md`](docs/architecture.md): what it is, the decisions and their trade-offs, the roadmap.
- [`docs/design.md`](docs/design.md): interfaces, data model, workflow, tests (the phase 1 baseline).
- [`docs/design-phase2.md`](docs/design-phase2.md): each hardening component, what was verified and what was not.
- [`docs/design-phase2b.md`](docs/design-phase2b.md), [`docs/design-phase3.md`](docs/design-phase3.md): designs for the work not started yet (multi-node, real CI, secrets, roles, cost budgets, operations; then a second scenario, design intent, memory, evaluation).
- [`docs/handoff.md`](docs/handoff.md): where things stand, how to run everything, known pitfalls.

Both are written in Chinese. In short: a modular monolith in Python; [Temporal](https://temporal.io)
for durable orchestration; Postgres as the single source of truth with an append-only event log;
one container per task as the sandbox (Docker or Kubernetes, optionally under gVisor); the agent is
a pluggable backend (the first one wraps the Codex CLI).

## Quickstart

Requirements: Linux, Docker with the compose plugin, Python 3.12, git, and a logged-in
[Codex CLI](https://github.com/openai/codex) (`codex login`).

```bash
make venv      # create .venv and install
make up        # start Postgres and the Temporal dev server
make image     # build the sandbox image
make migrate   # create tables

make api       # terminal 1: API on http://localhost:8000
make worker    # terminal 2: workflow worker
```

Submit a task and follow it:

```bash
.venv/bin/sca submit \
  --repo /path/to/your/repo.git \
  --instruction "Fix the failing test in test_calc.py" \
  --verify "python -m pytest -q" \
  --follow

.venv/bin/sca patch <task_id>                      # see the result
.venv/bin/sca review <task_id> request-changes -m "Also handle negative numbers"
.venv/bin/sca review <task_id> approve
```

Or open <http://localhost:8000> for the web page (token: `dev-token`). The Temporal UI is at
<http://localhost:8233>.

## Portal demo (OpenCode eval → fix → rebuild)

### Portal 快速启动

两种模式都使用 Python 3.12 和 Git。打开脚本打印的 `http://127.0.0.1:8000/portal`，
用首次启动时生成的令牌登录。源码模式将令牌保存在 `.data/portal.env`，终端显示保存位置。

```bash
# Docker 模式，首次安装并启动
bash scripts/portal_demo.sh setup --mode docker
# Docker 模式，后续启动
bash scripts/portal_demo.sh up --mode docker

# 源码模式，首次 up 会准备依赖并启动全部本机服务
bash scripts/portal_demo.sh up --mode source
bash scripts/portal_demo.sh status
bash scripts/portal_demo.sh down
```

也可设置 `SCA_RUNTIME_MODE=docker` 或 `SCA_RUNTIME_MODE=source`。
省略两种设置时，脚本优先使用可用的 Docker，否则会提示并进入源码模式。
源码模式的系统依赖和 API 示例见 [运维文档](docs/operations.md#源码模式)。
Windows 用户可直接运行 `.\scripts\portal_source.ps1 up`，默认使用 DeepSeek Harness。
[Windows 源码模式指南](docs/windows.md)

| 能力 | Docker 模式 | 源码模式 |
| --- | --- | --- |
| 任务隔离 | 每任务一个容器，支持出口白名单 | 本机进程，无隔离，出口白名单不生效 |
| OpenCode 冠军覆盖 | 支持运行时覆盖和构建固化 | 支持运行时覆盖，固化需要 Docker |
| Kubernetes | 独立部署可选用 K8s 沙箱驱动 | 使用本机沙箱 |

**本地源码模式：任务在本机直接运行，无隔离，仅限个人试用**

### 只看历史

在已有历史数据的机器上启动 Portal。Docker 模式首次使用时先运行上面的 `setup`。

```bash
bash scripts/portal_demo.sh up --mode docker
# 或使用本机源码模式
bash scripts/portal_demo.sh up --mode source
curl -fsS http://127.0.0.1:8000/v1/portal/config
curl -fsS http://127.0.0.1:8000/v1/tasks
```

在浏览器打开 `http://127.0.0.1:8000/portal`。新会话默认进入演示租户 `eval` 的任务历史，
无需输入令牌或 DeepSeek API Key。顶部「只读访客模式 · 登录」可切换到令牌登录。缺少模型密钥时
仍可查看轨迹和补丁，登录后可在概览配置密钥以恢复提交。[访客模式配置与风险](docs/operations.md#只读访客模式)

从 Linux 服务器把任务、项目、评测和演进记录带到 Windows 源码实例时，使用
[历史导出与导入](docs/history-transfer.md)。

### Docker 演示环境

在 Linux 或启用 Docker Desktop 的 macOS 上，准备 Docker Compose、
至少 15 GB 可用磁盘和足够内存（OpenCode 单次构建上限 2.5 GB，其他服务另需内存）。
macOS 先安装 GNU timeout：`brew install coreutils`。Apple Silicon 使用 Linux arm64
镜像和该架构的 OpenCode 构建。

```bash
make portal-setup
```

首次运行自动创建 `.env` 和随机网关密钥，并准备数据库、镜像、OpenCode 2.3.0
缺陷基线后启动 Portal。连接管理员令牌后，可在概览页填写 DeepSeek API Key；
密钥保存在本机 `.env`，保存时会重启模型网关。也可在启动前手动填写 `.env`。
脚本打印 `http://127.0.0.1:8000/portal` 和随机登录令牌；可在首次运行时通过
`PORTAL_TOKEN` 指定令牌（仅字母、数字、下划线和连字符）。重跑会保留已有令牌。
在 Portal 中可用 `opencode-demo` 验证文件工具修复，也可用 `opencode-prompt` 让 OpenCode
根据评测证据迭代系统提示词。macOS 的逐步操作见 [验证流程](Milestone/verification-flow.md#v4b-现场opencode-系统提示词自演进)。
本次真实任务、回放说明和逐事件归档见 [Portal 运行记录](Milestone/portal-run-2026-11-19.md)。

```bash
make portal-up                       # 启动 API 与 worker
make portal-status                   # 服务、网关和版本
bash scripts/portal_demo.sh logs      # 最近日志
make portal-down                     # 停止 API 与 worker
bash scripts/portal_demo.sh down --all # 同时停止 Compose，保留数据库卷
```

启动配置保存在 `.data/portal.env`，PID 与日志在 `.data/run/`，OpenCode 缓存、源码和版本在
`.data/opencode/`。不要分享 `.env`、`.data/portal.env` 或日志。远程访问使用
`ssh -L 8000:127.0.0.1:8000 host`，然后在本地浏览器打开 Portal。演示使用真实模型，会产生费用。

## Phase 2

Phase 2 replaces or strengthens one component at a time behind the phase 1 interfaces, using
existing open-source software wherever possible. Optional services are compose profiles.

| Area | What it adds | Built on | Enable with |
| --- | --- | --- | --- |
| Sandbox | No direct route out; egress only to allowlisted hosts. Stronger isolation. A Kubernetes driver. | Squid, gVisor, Kubernetes | on by default; `SCA_EGRESS_ALLOW`, `SCA_SANDBOX_RUNTIME=runsc` |
| Credentials | The model key never enters the sandbox; each agent turn gets a short-lived key. | LiteLLM | `--profile gateway`, `SCA_MODEL_GATEWAY_URL` |
| Identity | OIDC login, tenant isolation enforced by the database, per-tenant quotas. | Dex, PyJWT, Postgres row-level security | `--profile oidc`, `SCA_OIDC_ISSUER`, `SCA_API_DATABASE_URL` |
| Orchestration | Idle tasks release their sandbox; pause/resume; tasks survive a killed worker. | Temporal (on Postgres) | `--profile temporal-pg` |
| Storage | Artifacts in S3-compatible storage; backup/restore; old event logs archived. | SeaweedFS, boto3 | `--profile s3`, `SCA_OBJECT_STORE=s3` |
| Verification | Several checks in parallel; the project's own CI reports back by HTTP. | your CI | `verify_commands`, `ci` in the task spec |
| Observability | One trace per task across API, workflow and activities; metrics; token budgets; pushed event streams. | OpenTelemetry, Prometheus, Postgres LISTEN/NOTIFY | `SCA_OTLP_ENDPOINT`, `GET /metrics`, `max_tokens` |
| Parallel work | A task split into subtasks runs them at once in separate sandboxes and merges the results; conflicts go to the reviewer. | Temporal child workflows, `git merge-tree` | `subtasks` in the task spec |
| Speed | Repository mirrors and pip downloads are cached. | git reference repositories, pip | on by default |

```bash
make up-all    # also start the optional services (needs a .env with provider keys for the gateway)
```

## Phase 3

| Area | What it adds | Enable with |
| --- | --- | --- |
| Second scenario | The kernel no longer knows about coding: a `data analysis` kind (upload files, get a reproducible report) runs on the same task engine. | `"kind": "analysis"`, `POST /v1/files` |
| Design intent | An OpenAPI spec, a baseline screenshot or a linter config becomes context for the agent and executable checks. | `intents` in any task spec |
| Memory | Project conventions distilled from reviewer feedback (or written by people), approved by a human, given to later tasks. | `/v1/memories`, `accept_memories` in a review |
| Evaluation | Hidden acceptance checks, evaluation suites, per-case comparison of models / prompt versions, a regression gate. | `sca eval run|show|compare|gate`, `evals/` |

## Phase 6: evolution and commercial basics

[2026-11-22 发布说明与现场验证记录](docs/release-1122.md)

| Area | What it adds | Enable with |
| --- | --- | --- |
| Self-evolution | OpenCode prompts and tool descriptions evolve as overlays: reflect on dev failures, propose candidates, gate on a holdout split for quality or cost, promote a champion. Schedules, approvals, percentage rollouts, rollback, solidifying a champion into a built version, and harvesting real tasks as cases. | `/v1/evolutions`, `/v1/champions`, `/v1/evolution-schedules`, Portal → 自动演进 |
| 项目与线程 | 每个项目保存仓库、指令和执行默认值，按状态查看线程，并汇集项目文件、记忆和用量。 | `/v1/projects`，Portal → 项目，[使用说明](docs/projects.md) |
| Multi-tenant business | Tenants on plans (cost and concurrency limits), suspension, members and roles, tenant API tokens, operator-issued first tokens, per-tenant usage metering and monthly CSV billing export. | `SCA_PLATFORM_ADMIN_TOKEN`, `/v1/admin/*`, `/v1/tenant`, `/v1/usage` ([docs/operations.md](docs/operations.md)) |
| Payments | Stripe Checkout for monthly plans, a billing portal, invoice history, and monthly usage overages. | `/v1/tenant/billing`, `/v1/admin/billing`, `sca admin report-usage-to-stripe` ([setup](docs/operations.md#stripe-订阅与付款)) |
| Code hosting | GitHub / Gitea: tasks from issues, pull requests with review and CI status, outcomes written back. | `/v1/integrations/scm`, [docs/scm.md](docs/scm.md) |
| Best-of-N | N independent attempts from one prepared snapshot; the platform picks the cheapest verified one, or a human picks. | `"best_of": {"n": 3}` |
| Decoupled subtasks | Subtasks with `depends_on` and path scopes; auto planning; impact analysis of the published commit. | `subtasks`, `"plan": "auto"`, `/v1/tasks/{id}/impact` ([docs/decouple.md](docs/decouple.md)) |
| 插话 | 在任务运行时补充要求，可立即打断当前轮，或在审核时要求继续修改。 | Portal → 任务 → 插话，`POST /v1/tasks/{id}/messages` ([使用说明](docs/operations.md#运行中插话)) |
| Tools | Tenant MCP tool registry with team grants, and a built-in knowledge-base MCP server with citations. | `/v1/tools`, `/v1/knowledge`, `"tools": [...]` ([docs/tools.md](docs/tools.md)) |
| Design intents | Figma, OpenAPI and screenshot intents turned into agent context and checks. | `intents` ([docs/intents.md](docs/intents.md)) |
| Brain/hands | OpenCode brain outside the sandbox, shell over a PTY daemon, a live read-only terminal. | `opencode-brain`, `SCA_SANDBOX_HANDD=true`, `/v1/tasks/{id}/terminal` |

Benchmarks for evolution live in `evals/opencode-evolve*`. See [Basic commercial readiness](docs/commercial-readiness.md)
for the acceptance checklist, API and Portal entry points, and live smoke commands.

### Agent 后端

| `agent_backend` | 运行方式 |
| --- | --- |
| `codex` | 沙箱内运行 Codex CLI |
| `opencode` | 沙箱内运行 OpenCode |
| `opencode-brain` | 沙箱外运行 OpenCode，工具在沙箱内执行 |
| `pi` | 使用 `sca-sandbox-pi:latest` 运行 Pi |
| `dsh` | Docker 支持将 DeepSeek Harness 运行时挂载到任务镜像，源码模式使用项目本地安装 |
| `fake` | 使用脚本化 Agent 验证任务流程 |

#### 使用 DeepSeek Harness

`make portal-setup` 会构建默认的 DSH 镜像。也可单独构建。

```bash
make image
docker build -f deploy/dsh/sandbox.Dockerfile -t sca-sandbox-dsh:latest deploy/dsh
```

在 Worker 启用 LiteLLM 网关后，在 Portal「提交任务」选 DeepSeek Harness，模型填 `deepseek-flash`
或 `deepseek-v4-pro`。省略模型时使用 `SCA_DEFAULT_MODEL`。如需设为全局默认后端，配置
`SCA_AGENT_BACKEND=dsh` 和 `SCA_DEFAULT_MODEL=deepseek-flash` 后重启 API 与 Worker。
Docker 模式下，在「沙箱镜像（可选）」填写 `sca-sandbox:latest` 即可挂载 DSH 运行时。
省略镜像时使用 `SCA_DSH_SANDBOX_IMAGE`，默认为 `sca-sandbox-dsh:latest`。下例通过 API
提交使用挂载运行时的 Docker 任务。

```bash
curl -fsS "$SCA_API_URL/v1/tasks" -H "Authorization: Bearer $SCA_API_TOKEN" --json @- <<'JSON'
{"kind":"coding","spec":{"repo_url":"https://example.com/repo.git",
 "instruction":"为计算函数补边界测试","verify_command":"pytest -q",
 "agent_backend":"dsh","image":"sca-sandbox:latest","model":"deepseek-flash"}}
JSON
```

`SCA_DSH_GATEWAY_PROTOCOL` 默认 `auto`。直连 LiteLLM 使用 `/v1/messages` 和 `x-api-key`。
启用 SCA 网关限流代理时使用 `/v1/chat/completions` 和 Bearer 认证。可用 `messages` 或 `openai`
指定协议。DSH 使用每轮虚拟密钥，关闭内置网页搜索、抓取和遥测。后续轮次从 `/agent_home/dsh`
恢复会话。仓库工单的默认后端与模型可在 Portal 的 SCM 配置中设置。[工单配置](docs/scm.md#通过工单触发任务)

自建镜像时同步设置 `SCA_DSH_SANDBOX_IMAGE`、`SCA_DSH_VERSION` 和构建参数 `DSH_VERSION`。
上游包声明 Node ≥24.9，预构建镜像使用 Node 22.22.2。部署时需验证完整 headless 运行及续接。
[上游 CLI 包元数据](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.5/apps/cli/package.json#L186-L188)

#### Docker 运行时磁盘空间

DSH 的 Docker 运行时默认上限为 2,000 MB，可通过 `SCA_NODE_RUNTIME_MAX_MB` 调整。
Pi 默认为 270 MB，可通过 `SCA_PI_NODE_RUNTIME_MAX_MB` 调整。上限包含共享的 Node.js，
安装前 `SCA_DATA_DIR`（默认 `.data`）所在磁盘需要至少「上限加 1,000 MB」的可用空间。
例如，重启 Portal 时可以将 DSH 上限设为 2,400 MB。

```bash
bash scripts/portal_demo.sh down
SCA_NODE_RUNTIME_MAX_MB=2400 bash scripts/portal_demo.sh up --mode docker
```

项目可保存默认镜像。新建评测或演进时，可在套件的 `suite.yaml` 中设置 `defaults.image`。
验证使用挂载运行时的 DSH 任务，并确认排队、执行中、待恢复及后续提交的任务均已配置为
使用其他镜像后，检查 Docker 中使用旧镜像的容器。

```bash
docker ps -a --filter ancestor=sca-sandbox-dsh:latest
```

确认查询未列出容器后，运行 `docker image rm sca-sandbox-dsh:latest` 回收磁盘空间。

## Tests

```bash
make test        # unit + integration (needs `make up` and `make image`)
make e2e         # full stack with a scripted agent, no model needed
make e2e-live    # against the optional services (S3, OIDC, Temporal on Postgres)
make e2e-codex   # full stack with the real Codex agent (spends tokens)
```

Tests marked `k8s` need a local [kind](https://kind.sigs.k8s.io) cluster named `sca`
(`deploy/kind/cluster.yaml`); they skip without it.

## Limits

- **Single machine.** The worker hands the workspace to the sandbox through a host directory, so
  worker and sandboxes must share a filesystem. The Kubernetes driver therefore works on one node
  only.
- Run it for people and repositories you would let run code on that machine. The sandbox is
  network-restricted and can run under gVisor, but the default runtime is the plain container
  runtime.
- No dashboards or alerts.

[`docs/design-phase2.md` §10](docs/design-phase2.md) has the full list.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

[MIT](LICENSE)
