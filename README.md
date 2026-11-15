# SuperCloudAgents

> 🚧 Early stage. It runs end to end on a single machine, with the hardening described under
> [Phase 2](#phase-2). It is not yet a multi-node production system; see [Limits](#limits).

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
- No dashboards, alerts, roles within a tenant, or cost-based (as opposed to token-based) budgets.

[`docs/design-phase2.md` §10](docs/design-phase2.md) has the full list.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

[MIT](LICENSE)
