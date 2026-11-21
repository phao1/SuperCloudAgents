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

在 Linux 或启用 Docker Desktop 的 macOS 上，准备 Python 3.12、Git、Docker Compose、
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

配置保存在 `.data/portal.env`，PID 与日志在 `.data/run/`，OpenCode 缓存、源码和版本在
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

| Area | What it adds | Enable with |
| --- | --- | --- |
| Self-evolution | OpenCode prompts and tool descriptions evolve as overlays: reflect on dev failures, propose candidates, gate on a holdout split for quality or cost, promote a champion. Schedules, approvals, percentage rollouts, rollback, solidifying a champion into a built version, and harvesting real tasks as cases. | `/v1/evolutions`, `/v1/champions`, `/v1/evolution-schedules`, Portal → 自动演进 |
| Multi-tenant business | Tenants on plans (cost and concurrency limits), suspension, members and roles, tenant API tokens, operator-issued first tokens, per-tenant usage metering and monthly CSV billing export. | `SCA_PLATFORM_ADMIN_TOKEN`, `/v1/admin/*`, `/v1/tenant`, `/v1/usage` ([docs/operations.md](docs/operations.md)) |
| Payments | Stripe Checkout for monthly plans, a billing portal, invoice history, and monthly usage overages. | `/v1/tenant/billing`, `/v1/admin/billing`, `sca admin report-usage-to-stripe` ([setup](docs/operations.md#stripe-订阅与付款)) |
| Code hosting | GitHub / Gitea: tasks from issues, pull requests with review and CI status, outcomes written back. | `/v1/integrations/scm`, [docs/scm.md](docs/scm.md) |
| Best-of-N | N independent attempts from one prepared snapshot; the platform picks the cheapest verified one, or a human picks. | `"best_of": {"n": 3}` |
| Decoupled subtasks | Subtasks with `depends_on` and path scopes; auto planning; impact analysis of the published commit. | `subtasks`, `"plan": "auto"`, `/v1/tasks/{id}/impact` ([docs/decouple.md](docs/decouple.md)) |
| Tools | Tenant MCP tool registry with team grants, and a built-in knowledge-base MCP server with citations. | `/v1/tools`, `/v1/knowledge`, `"tools": [...]` ([docs/tools.md](docs/tools.md)) |
| Design intents | Figma, OpenAPI and screenshot intents turned into agent context and checks. | `intents` ([docs/intents.md](docs/intents.md)) |
| Brain/hands | OpenCode brain outside the sandbox, shell over a PTY daemon, a live read-only terminal. | `opencode-brain`, `SCA_SANDBOX_HANDD=true`, `/v1/tasks/{id}/terminal` |

Benchmarks for evolution live in `evals/opencode-evolve*`. See [Basic commercial readiness](docs/commercial-readiness.md)
for the acceptance checklist, API and Portal entry points, and live smoke commands.

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
