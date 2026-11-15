# SuperCloudAgents 详细设计（一期 MVP）

> 对应 [`architecture.md`](architecture.md) V2.0。本文是一期的实现基线：代码结构、接口、数据模型、工作流、测试。
> 上一版（19 个服务的设计）保存在 [`archive/design-v1.md`](archive/design-v1.md)。

## 1 范围

一期只交付一条链路：

```text
提交任务（仓库 + 需求 + 验证命令）
  → 克隆仓库、创建沙箱
  → Agent 在沙箱里编码一轮
  → 提交改动、运行验证
  → 验证失败则自动修复（有上限）
  → 生成补丁，可选推送分支
  → 等待人确认 / 提修改意见 / 拒绝
  → 结束并销毁沙箱
```

每个标了「二期」的地方，一期都只保证接口在，不保证实现。

## 2 代码结构

一个仓库、一个 Python 包 `sca`。

```text
pyproject.toml
Makefile
deploy/
  docker-compose.yml        # Postgres、Temporal 开发服务器
  sandbox/Dockerfile        # 沙箱镜像
src/sca/
  config.py                 # 全部配置项（环境变量，前缀 SCA_）
  core/
    models.py               # 领域模型与枚举，全仓共用
    interfaces.py           # 可替换组件的接口
    errors.py               # 错误类型
  db/
    schema.py               # 表定义
    engine.py               # 连接与会话
    repo.py                 # 任务、事件、制品的读写
    migrations/             # Alembic
  storage/local.py          # 对象存储的本地目录实现
  sandbox/docker.py         # 沙箱的 Docker 实现
  workspace/git.py          # 工作区：克隆、提交、补丁、推送
  agents/
    codex.py                # Agent 后端：Codex CLI
    fake.py                 # Agent 后端：测试用脚本
  kinds/coding/
    spec.py                 # coding 任务的规格
    prompts.py              # 提示词模板
    activities.py           # 每一步的实际动作
    workflow.py             # 步骤顺序
  orchestration/
    client.py               # 启动工作流、发信号
    worker.py               # Worker 进程入口
  api/
    app.py                  # 应用装配
    auth.py                 # 鉴权
    routes.py               # 接口
    static/index.html       # 单页界面
  cli.py                    # 命令行 `sca`
tests/
  unit/                     # 不依赖外部进程
  integration/              # 依赖 Postgres 或 Docker
  e2e/                      # 依赖全部组件
```

依赖方向只有一个：`api`、`orchestration`、`kinds` 依赖 `core`、`db` 和各接口实现；`core` 不依赖任何其他模块。`kinds/coding` 之外的模块里不出现「仓库」「测试命令」这类场景概念。

## 3 领域模型

定义在 `core/models.py`，是全仓的共同语言。

### 3.1 任务

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID | 任务标识。 |
| `tenant_id` | 字符串 | 租户。一期恒为 `default`。 |
| `kind` | 字符串 | 任务类型。一期只有 `coding`。 |
| `spec` | JSON | 任务规格，结构由 `kind` 决定。 |
| `state` | 枚举 | 见 3.2。 |
| `state_reason` | 字符串，可空 | 失败或取消的原因。 |
| `result` | JSON | 结果摘要：分支名、提交、补丁制品名、验证是否通过。 |
| `usage` | JSON | 累计 Token 用量。 |
| `idempotency_key` | 字符串，可空 | 同一租户内唯一，用于重复提交去重。 |
| `created_at` / `updated_at` | 时间 | — |

### 3.2 任务状态

`QUEUED`、`PROVISIONING`、`RUNNING`、`VERIFYING`、`AWAITING_REVIEW`、`SUCCEEDED`、`FAILED`、`CANCELLED`。

迁移关系见架构文档 4.3 节。后三个是终态。状态只由工作流通过活动改写。

### 3.3 coding 任务规格

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `repo_url` | 必填 | Git 地址，或 Worker 所在机器上的路径。 |
| `base_ref` | `main` | 基线分支或提交。 |
| `instruction` | 必填 | 需求描述。 |
| `verify_command` | 可空 | 验证命令，在工作区根目录用 `bash -lc` 执行。为空则跳过验证。 |
| `setup_command` | 可空 | 环境准备命令，在 Agent 开始前执行一次。 |
| `push` | `false` | 是否把结果分支推送回 `repo_url`。 |
| `max_fix_attempts` | `2` | 验证失败后的自动修复次数上限。 |
| `image` | 配置默认值 | 沙箱镜像。 |
| `model` | 配置默认值 | 模型名，交给 Agent 后端。 |
| `turn_timeout_s` | `1800` | 单轮 Agent 的超时。 |

### 3.4 事件

事件只追加。每个任务内 `seq` 从 1 递增且不重复。

| 类型 | 何时产生 | 负载要点 |
| --- | --- | --- |
| `task.created` | 提交 | 规格 |
| `task.state_changed` | 每次状态迁移 | `from`、`to`、`reason` |
| `workspace.prepared` | 克隆完成 | `base_sha`、`branch` |
| `sandbox.created` / `sandbox.destroyed` | 沙箱创建与销毁 | `sandbox_id` |
| `setup.completed` | 准备命令结束 | `exit_code`、输出尾部 |
| `agent.turn_started` | 一轮开始 | `turn`、`prompt` |
| `agent.message` | Agent 说了一段话 | `text` |
| `agent.command` | Agent 执行了一条命令 | `command`、`exit_code`、输出尾部 |
| `agent.file_change` | Agent 改了文件 | 文件与改动类型 |
| `agent.usage` | 一轮的 Token 用量 | 输入、缓存输入、输出 |
| `agent.error` | Agent 报错 | `message` |
| `agent.turn_completed` | 一轮结束 | `turn`、`final_message` |
| `workspace.checkpointed` | 改动已提交 | `commit`、`changed` |
| `verification.completed` | 验证结束 | `passed`、`exit_code`、输出尾部、日志制品名 |
| `artifact.published` | 补丁或分支已发布 | 制品名、`pushed` |
| `review.requested` | 进入待确认 | `verification_passed` |
| `review.submitted` | 人做出决定 | `decision`、`feedback` |

事件负载里的命令输出一律截断到尾部 4KB，完整内容放对象存储。

## 4 可替换组件的接口

定义在 `core/interfaces.py`。调用方只依赖接口。

### 4.1 沙箱

```python
class SandboxProvider(Protocol):
    async def create(self, name: str, spec: SandboxSpec) -> str: ...
    def exec(self, sandbox_id: str, argv: list[str], *, workdir: str | None = None,
             env: dict[str, str] | None = None, stdin: bytes | None = None,
             timeout_s: float | None = None) -> AsyncIterator[ExecEvent]: ...
    async def exists(self, sandbox_id: str) -> bool: ...
    async def destroy(self, sandbox_id: str) -> None: ...
```

- `create` 按 `name` 幂等：同名沙箱已存在就直接返回它。活动被重试时不会建出第二个。
- `exec` 流式返回：若干条 `ExecOutput(stream, text)`，最后一条是 `ExecExit(exit_code, timed_out)`。超时会杀掉沙箱里的整个进程组。
- `destroy` 幂等：沙箱不存在也算成功。
- `SandboxSpec` 包含：镜像、挂载列表、环境变量、CPU 与内存上限、是否联网、标签。

**Docker 实现**：通过 `docker` 命令行子进程驱动。容器以 `sleep infinity` 常驻，用宿主机当前用户的 UID 运行，不挂载 Docker 套接字，带 `--init` 以便回收子进程。

### 4.2 Agent 后端

```python
class AgentBackend(Protocol):
    name: str
    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions: ...
    def run_turn(self, sandbox: SandboxProvider, sandbox_id: str, ctx: AgentContext,
                 prompt: str, *, session_id: str | None,
                 timeout_s: float) -> AsyncIterator[AgentEvent]: ...
```

- `sandbox_additions` 让后端声明它需要沙箱额外提供什么（挂载、环境变量）。内核不需要知道某个后端的二进制放在哪。
- `run_turn` 运行一轮。`session_id` 为空表示新会话；否则续接。
- `AgentEvent` 是后端无关的统一事件：`session_started`、`message`、`command`、`file_change`、`usage`、`error`、`turn_completed`。每个事件保留后端的原始数据在 `raw` 里。
- 一轮以 `turn_completed` 正常结束；进程非零退出或超时则抛 `AgentTurnError`。

**Codex 实现**：

- 在沙箱内执行 `codex exec --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -m <model> -C /workspace -`，指令从标准输入传入。续接时用 `codex exec resume <session_id>`。
- 关闭 Codex 自带的沙箱是因为外层已经是容器，内层沙箱在容器里无法工作。
- 需要的挂载：Codex 二进制目录（只读）；每个任务独立的 `CODEX_HOME` 目录（保存会话历史）；宿主机的 `auth.json`（单文件挂载）。
- 把 Codex 输出的每行 JSON 映射成统一事件；不认识的行忽略并保留在日志里。

**测试用实现**：在沙箱里执行任务规格 `instruction` 中约定的一段 shell 脚本，用来在没有模型的情况下测试整条链路。

### 4.3 对象存储

```python
class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = ...) -> str: ...
    async def get(self, key: str) -> bytes: ...
```

键的格式是 `tasks/{task_id}/{name}`。一期写入本地目录，二期换 S3。

### 4.4 鉴权

```python
class Authenticator(Protocol):
    async def authenticate(self, token: str | None) -> Principal: ...
```

`Principal` 带 `tenant_id` 和 `subject`。一期的实现是配置里的静态令牌表。

## 5 工作区与凭证

这是安全边界所在，单独说明。

每个任务在 Worker 的数据目录下有三样东西：

```text
{data_dir}/tasks/{task_id}/
  mirror.git/     # 裸仓库。只在沙箱外，从不挂载
  workspace/      # 工作副本。挂载到沙箱的 /workspace
  agent_home/     # Agent 的会话历史。挂载到沙箱
```

| 步骤 | 在哪执行 | 做什么 |
| --- | --- | --- |
| 准备 | 沙箱外 | 用仓库凭证把 `repo_url` 克隆成 `mirror.git`；再从 `mirror.git` 克隆出 `workspace`，建任务分支 `sca/{task_id 前 8 位}`。并在工作副本的本地排除规则里加入常见缓存目录（如 `__pycache__/`），避免验证命令留下的文件被提交。 |
| 提交 | **沙箱内** | `git add -A` 后提交。没有改动就跳过。 |
| 取回 | 沙箱外 | `mirror.git` 从 `workspace` 抓取 `HEAD` 到任务分支。 |
| 补丁 | 沙箱外 | 在 `mirror.git` 里比较基线提交与任务分支。 |
| 推送 | 沙箱外 | 从 `mirror.git` 把任务分支推送到 `repo_url`。 |

为什么这样绕：`workspace/.git` 对沙箱可写，沙箱里的代码可以往里面放钩子或恶意配置。所以**沙箱外的进程从不在 `workspace` 里执行会触发钩子或读取其配置的 Git 操作**，只把它当作一个不受信任的远端来抓取。带凭证的操作都发生在 `mirror.git` 上。

## 6 数据库

PostgreSQL，四张表。迁移由 Alembic 管理。

```sql
CREATE TABLE tasks (
  id               uuid PRIMARY KEY,
  tenant_id        text NOT NULL,
  kind             text NOT NULL,
  spec             jsonb NOT NULL,
  state            text NOT NULL,
  state_reason     text,
  result           jsonb NOT NULL DEFAULT '{}',
  usage            jsonb NOT NULL DEFAULT '{}',
  idempotency_key  text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ix_tasks_tenant_created ON tasks (tenant_id, created_at DESC);

CREATE TABLE events (
  id          bigserial PRIMARY KEY,
  task_id     uuid NOT NULL REFERENCES tasks(id),
  seq         bigint NOT NULL,
  type        text NOT NULL,
  payload     jsonb NOT NULL DEFAULT '{}',
  dedup_key   text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, seq),
  UNIQUE (task_id, dedup_key)
);

CREATE TABLE sessions (
  id                  uuid PRIMARY KEY,
  task_id             uuid NOT NULL REFERENCES tasks(id),
  backend             text NOT NULL,
  backend_session_id  text,
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE artifacts (
  id          uuid PRIMARY KEY,
  task_id     uuid NOT NULL REFERENCES tasks(id),
  name        text NOT NULL,
  content_type text NOT NULL,
  uri         text NOT NULL,
  size_bytes  bigint NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (task_id, name)
);
```

两条写入规则：

- **事件序号**：追加事件时先锁住对应的任务行，再取该任务当前最大 `seq` 加一。并发写同一任务会排队，不会重号。
- **事件去重**：活动可能被重试。每次追加都带 `dedup_key`；键冲突时静默跳过。这样重试不会产生重复事件。

`sessions` 与任务是一对多，一期每个任务只有一行。这是为二期多 Agent 留的。

## 7 工作流

### 7.1 结构

| 项 | 值 |
| --- | --- |
| 工作流 | `CodingTaskWorkflow` |
| 工作流标识 | `task-{task_id}` |
| 任务队列 | `sca-coding` |
| 输入 | `task_id`（其余从数据库读） |
| 信号 | `submit_review(decision, feedback)` |
| 取消 | 用 Temporal 的取消请求 |

### 7.2 步骤

```python
async def run(self, task_id):
    try:
        spec = await load_task(task_id)
        await set_state(PROVISIONING)
        ws = await prepare_workspace(task_id)
        await create_sandbox(task_id)
        if spec.setup_command:
            await run_setup(task_id)

        prompt, turn, fixes, session_id = initial_prompt(spec), 0, 0, None
        while True:
            turn += 1
            await set_state(RUNNING)
            session_id = (await run_agent_turn(task_id, turn, prompt, session_id)).session_id
            await checkpoint_workspace(task_id, turn)

            await set_state(VERIFYING)
            v = await run_verification(task_id, turn)
            if not v.passed and fixes < spec.max_fix_attempts:
                fixes += 1
                prompt = fix_prompt(v)
                continue

            await publish(task_id, turn, v.passed)
            await set_state(AWAITING_REVIEW)
            review = await self.wait_for_review()
            if review.decision == "approve":
                return await set_state(SUCCEEDED)
            if review.decision == "reject":
                return await set_state(CANCELLED, reason=review.feedback)
            prompt, fixes = feedback_prompt(review.feedback), 0
    except CancelledError:
        await set_state(CANCELLED, reason="cancelled by user")
        raise
    except ActivityError as e:
        await set_state(FAILED, reason=str(e.cause))
        raise
    finally:
        await destroy_sandbox(task_id)
```

工作流代码里没有任何 I/O，所有带 I/O 的事情都在活动里。

### 7.3 活动

| 活动 | 超时 | 重试 | 说明 |
| --- | --- | --- | --- |
| `load_task` | 30 秒 | 默认 | 读规格。 |
| `set_state` | 30 秒 | 默认 | 改状态并追加 `task.state_changed`，同一事务。 |
| `prepare_workspace` | 10 分钟 | 3 次 | 目录已存在则先清掉再克隆。地址或引用无效时抛不可重试错误。 |
| `create_sandbox` | 5 分钟 | 3 次 | 按名字幂等。 |
| `run_setup` | 15 分钟 | 不重试 | 非零退出则任务失败。 |
| `run_agent_turn` | 规格里的轮超时 + 1 分钟 | 2 次 | 每收到一个事件就发一次心跳；心跳超时 2 分钟。事件边产生边写库。 |
| `checkpoint_workspace` | 2 分钟 | 3 次 | 沙箱内提交，沙箱外抓取。 |
| `run_verification` | 30 分钟 | 不重试 | 完整输出存对象存储。命令失败是正常结果，不是活动失败。 |
| `publish` | 5 分钟 | 3 次 | 生成补丁制品；`push` 为真时推送分支；更新任务的 `result`。 |
| `destroy_sandbox` | 2 分钟 | 5 次 | 幂等。 |

`run_agent_turn` 被重试时，上一次尝试里已写入的事件保留（它们确实发生过），新尝试的事件用新的去重键。

### 7.4 提示词

三个模板，都在 `kinds/coding/prompts.py`：

- **首轮**：需求原文；说明工作目录、验证命令是什么；要求完成后不要自己提交或推送。
- **修复**：说明验证命令失败了，附退出码和输出尾部，要求修到通过。
- **人工反馈**：附上人的修改意见原文。

## 8 API

前缀 `/v1`，JSON。鉴权用 `Authorization: Bearer <token>`。错误体是 `{"error": {"code", "message"}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/tasks` | 提交任务。请求体 `{kind, spec}`。可带 `Idempotency-Key` 头；重复提交返回同一个任务。成功返回 201。 |
| `GET` | `/v1/tasks` | 任务列表，按创建时间倒序。参数 `state`、`limit`。 |
| `GET` | `/v1/tasks/{id}` | 任务详情。 |
| `GET` | `/v1/tasks/{id}/events` | 事件列表。参数 `after`（序号）、`limit`。 |
| `GET` | `/v1/tasks/{id}/events/stream` | 服务器推送事件流。支持 `Last-Event-ID` 断点续传；任务到终态后关闭。 |
| `POST` | `/v1/tasks/{id}/review` | 人工决定。请求体 `{decision, feedback}`，`decision` 取 `approve`、`request_changes`、`reject`。任务不在 `AWAITING_REVIEW` 时返回 409。 |
| `POST` | `/v1/tasks/{id}/cancel` | 取消。已到终态时返回 409。 |
| `GET` | `/v1/tasks/{id}/artifacts` | 制品列表。 |
| `GET` | `/v1/tasks/{id}/artifacts/{name}` | 下载制品。 |
| `GET` | `/healthz` | 健康检查，不鉴权。 |
| `GET` | `/` | 单页界面，不鉴权（页面里的请求仍需令牌）。 |

提交任务的顺序是：先在一个事务里写入任务行和 `task.created` 事件，再启动工作流。工作流标识由任务标识决定，所以「写库成功、启动失败后重试」不会启动两个工作流。

事件流在一期用轮询数据库实现（间隔 0.5 秒）。二期换成 Postgres 的通知机制。

## 9 命令行与界面

```text
sca submit --repo <url> --instruction <text> [--verify <cmd>] [--setup <cmd>] [--base main] [--push] [--follow]
sca list
sca show <task_id>
sca events <task_id> [--follow]
sca review <task_id> approve | request-changes -m <text> | reject [-m <text>]
sca cancel <task_id>
sca patch <task_id>
```

单页界面提供同样的能力：任务列表、事件时间线（实时）、查看补丁、确认与反馈按钮。它是一个不带构建步骤的静态 HTML 文件。

## 10 配置

全部来自环境变量，前缀 `SCA_`，可放在 `.env` 里。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SCA_DATABASE_URL` | `postgresql+asyncpg://sca:sca@localhost:5432/sca` | 数据库。 |
| `SCA_TEMPORAL_ADDRESS` | `localhost:7233` | Temporal。 |
| `SCA_TEMPORAL_NAMESPACE` | `default` | — |
| `SCA_DATA_DIR` | `./.data` | 任务目录与本地对象存储的根。 |
| `SCA_API_TOKENS` | `dev-token:default` | `令牌:租户` 列表，逗号分隔。 |
| `SCA_API_URL` | `http://localhost:8000` | 命令行访问的地址。 |
| `SCA_AGENT_BACKEND` | `codex` | `codex` 或 `fake`。 |
| `SCA_DEFAULT_MODEL` | `gpt-6.1-sol` | — |
| `SCA_SANDBOX_IMAGE` | `sca-sandbox:latest` | — |
| `SCA_SANDBOX_CPUS` / `SCA_SANDBOX_MEMORY_MB` | `1.5` / `1536` | 每个沙箱的资源上限。 |
| `SCA_CODEX_BIN_DIR` | 自动探测 | 宿主机上 Codex 二进制所在目录。 |
| `SCA_CODEX_AUTH_FILE` | `~/.codex/auth.json` | 宿主机上的 Codex 凭证文件。 |

## 11 本地运行

```bash
make up        # 启动 Postgres 与 Temporal
make image     # 构建沙箱镜像
make migrate   # 建表
make api       # 启动 API（前台）
make worker    # 启动 Worker（前台）
make test      # 单元与集成测试
make e2e       # 端到端测试
```

API 和 Worker 在一期直接跑在宿主机上。Worker 需要访问 Docker 和任务目录。

## 12 测试策略

| 层 | 依赖 | 覆盖什么 |
| --- | --- | --- |
| 单元 | 无 | 状态迁移规则、提示词、规格校验、Codex 输出解析（用录制的真实输出）、鉴权。 |
| 集成 | Postgres 或 Docker | 事件序号与去重；沙箱的创建、执行、超时、销毁；工作区的克隆、提交、补丁、推送；API 各接口。 |
| 工作流 | Temporal 测试环境 | 用假活动测流程分支：一次通过、修复后通过、修复用尽、人工反馈回合、拒绝、取消、活动失败。 |
| 端到端（假 Agent） | 全部组件 | 真实的 Temporal、Postgres、Docker；Agent 用测试脚本。稳定、快，适合每次改动后跑。 |
| 端到端（真 Agent） | 全部组件 + 模型 | 真实的 Codex。包含一次人工提修改意见的回合。这是一期的验收测试。 |

端到端测试用的被测仓库是测试自己创建的本地 Git 仓库，不依赖外部代码托管。

## 13 一期已知的不足

> 二期对下面各项的处理和验证结果见 [`design-phase2.md`](design-phase2.md)。本文其余部分描述的是一期基线；二期改动过的地方（沙箱网络默认受限、新增状态 `PAUSED`、新增接口和配置）以二期文档为准。

这些是有意留下的，二期按架构文档 8.2 节的顺序处理。

| 不足 | 影响 | 二期方案 |
| --- | --- | --- |
| 模型凭证挂载进沙箱 | 沙箱内代码可读到它 | 模型网关 + 任务级临时令牌 |
| 沙箱可任意联网 | 数据可被外发 | 默认拒绝 + 出口白名单 |
| 普通容器隔离 | 不能运行不受信任租户的任务 | gVisor 或微虚拟机 |
| Worker 与沙箱必须在同一台机器 | 不能横向扩展 | Kubernetes 沙箱实现 + 共享存储或在沙箱内克隆 |
| 事件流靠轮询 | 有 0.5 秒延迟，连接多时压数据库 | Postgres 通知机制 |
| 静态令牌鉴权 | 没有用户概念 | OIDC |
| 等待人工确认没有期限 | 沙箱一直占着资源 | 超时后销毁沙箱，确认时再重建 |
| 一轮中途崩溃要重跑整轮 | 浪费一轮的 Token | 轮内更细的续接 |
| 没有用量预算 | 任务可能花费超出预期 | 按任务与租户设上限 |
