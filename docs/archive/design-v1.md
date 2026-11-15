# 云端 Coding Agent 研发接管平台实现架构与技术详细设计

> 19 个服务 · 22 个代码仓 · 服务规格、数据与中间件、关键技术 · V1.0

| 项目 | 内容 |
| --- | --- |
| 文档编号 | IMPL-CA-2026-001 |
| 文档版本 | V1.0（实施基线） |
| 编制日期 | 2026-11-11 |
| 编制单位 | 平台架构组 |
| 服务/代码仓 | 19 个 / 22 个 |

## 1 文档说明与使用方式

### 1.1 目的

本文把《云端 Coding Agent 架构设计文档》落地为可实施的工程方案：明确服务数量、代码仓划分、开发语言与框架、数据库与中间件规格、每个服务的接口/数据/依赖/关键技术与验收标准。每个服务章节均为独立的代码生成输入。

### 1.2 与架构文档的关系

架构文档回答“系统由哪些层与能力构成”；本文回答“这些能力如何拆成可部署服务、如何建库建仓、如何实现关键算法”。两文的服务命名与边界一致，冲突时以实现设计为准并回写架构文档。

### 1.3 使用方式

- 按第 3 章的生成顺序选择服务，先落地 ca-contracts 契约仓，再生成服务代码。
- 将第 7 章对应服务小节整体作为代码生成上下文：元数据、API、表结构、类职责、配置、验收缺一不可。
- 第 26 章提供核心表 DDL，第 27 章提供 Topic/Key/Bucket 命名，第 28 章提供关键算法伪代码。
- 每个服务完成后按 7.x“验收标准”与第 31 章 DoD/CI 门禁验收，再进行下游服务。

### 1.4 约定

- 语言：后端统一 Python 3.12；前端工作台为 TypeScript（独立仓），不在本文逐页展开。
- 服务间：同步调用 REST/JSON（OpenAPI 3.1），异步事件 Kafka；高吞吐内部调用可增 gRPC，但首版以 REST 为准。
- 命名：服务 snake_case 包名，类 PascalCase，函数 snake_case，配置大写下划线。
- 所有接口前缀 /v1；错误遵循 RFC 7807；写接口支持 Idempotency-Key。

## 2 实施范围与架构映射

### 2.1 实施边界

首版实现覆盖：人类工作台后端、设计意图解析、任务与编排、检查点与恢复、沙箱与 Runtime、执行器、工具网关、缓存、记忆、知识、身份、验证、审计、自进化、可观测与模型网关。不包含：生产变更执行器（仅输出变更与门禁建议）、非研发类办公流程、模型训练。

### 2.2 架构模块到服务映射

| 架构模块/能力 | 落地服务 | 说明 |
| --- | --- | --- |
| 人类入口、任务下发、质量看板、审批 | ca-ingress、ca-task | 入口鉴权与任务状态机 |
| 设计稿/Token/意图解析 | ca-design | 设计意图与置信度 |
| 任务理解、解耦、DAG | ca-planning、ca-orchestration | 规划与两层调度上层 |
| 检查点、Stored Work、Watchdog | ca-checkpoint | 断点续存与恢复编排 |
| 沙箱调度、池、恢复门 | ca-sandbox | 两层调度下层与隔离执行 |
| Agent Runtime（脑） | ca-agent-runtime | 无状态推理循环 |
| 执行器、补丁、证据 | ca-executor | 代码与工具动作落地 |
| Tool 网关、工具域、鉴权 | ca-tool-gateway | 有状态“手” |
| 代码/依赖缓存、挂载 | ca-cache | 动态挂载与 COW |
| 记忆、知识、身份、审计 | ca-memory、ca-knowledge、ca-identity、ca-audit | 共享服务 |
| 验证网关与解析 | ca-verification | 结构化反馈与根因线索 |
| 自进化、评测、发布 | ca-evolution | 目标→评测→迭代→发布 |
| Trace/回放/成本/风险 | ca-observability | Collector+存储+查询 |
| 模型路由、预算、安全 | ca-model-gateway | 多模型接入与成本控制 |

## 3 服务与代码仓总览

### 3.1 数量结论

| 项目 | 数量 | 说明 |
| --- | --- | --- |
| 可独立部署服务 | 19 | 每个服务独立进程、独立镜像、独立 Helm Release |
| 代码仓 | 22 | 19 个服务仓 + 契约仓 + 部署仓 + 前端工作台仓 |
| PostgreSQL 数据库 | 16 | 按服务一库（schema 隔离），Runtime/Executor/Ingress/Sandbox 不落业务库 |
| Kafka Topic | 12 类（含死信） | 领域事件、审计、Trace、成本、评测 |
| Redis 命名空间 | 8 | 幂等、锁、配额、限流、会话指针、热缓存 |
| ClickHouse 表 | 4 | spans、cost_usage、quality_events、audit_index |
| Qdrant Collection | 3 | memory_short、memory_long、knowledge_docs |
| 对象存储 Bucket | 4 | artifacts、checkpoints、cache、eval |

### 3.2 服务清单

| 编号 | 服务 | 平面 | 端口 | 主数据库 | 副本(最小–最大) | SLO 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | ca-ingress | 控制面 | 8001 | 无 | 3–12 | 30ms |
| S02 | ca-task | 控制面 | 8002 | ca_task | 2–8 | 100ms |
| S03 | ca-design | 控制面 | 8003 | ca_design | 2–6 | 3s |
| S04 | ca-planning | 控制面 | 8004 | ca_planning | 2–6 | 5s |
| S05 | ca-orchestration | 控制面 | 8005 | ca_orch | 3–12 | 200ms |
| S06 | ca-checkpoint | 控制面 | 8006 | ca_ckpt | 3–9 | 200ms/60s 恢复 |
| S07 | ca-sandbox | 执行面 | 8007 | ca_sandbox | 2–6 | 50ms 调度 |
| S08 | ca-agent-runtime | 执行面 | 8008 | 无 | 10–200 | 8s/step |
| S09 | ca-executor | 执行面 | 8009 | 无 | 5–100 | 1s 开销 |
| S10 | ca-tool-gateway | 执行面 | 8010 | ca_tool | 3–20 | 20ms 开销 |
| S11 | ca-cache | 数据面 | 8011 | ca_cache | 3–12 | 500ms 挂载 |
| S12 | ca-memory | 共享 | 8012 | ca_memory | 3–12 | 80ms |
| S13 | ca-knowledge | 共享 | 8013 | ca_knowledge | 2–8 | 120ms |
| S14 | ca-identity | 共享 | 8014 | ca_identity | 3–6 | 10ms |
| S15 | ca-verification | 验证 | 8015 | ca_verify | 2–8 | 100ms |
| S16 | ca-audit | 共享 | 8016 | ca_audit | 3–9 | 50ms |
| S17 | ca-evolution | 横切 | 8017 | ca_evolution | 2–4 | 异步任务 |
| S18 | ca-observability | 横切 | 8018 | ca_obs_meta + ClickHouse | 3–12 | 200ms/1s |
| S19 | ca-model-gateway | 共享 | 8019 | ca_model | 4–30 | 首 Token 800ms |

### 3.3 代码仓规划

| 编号 | 仓库 | 语言/形态 | 内容 |
| --- | --- | --- | --- |
| R01 | ca-contracts | Python 包 | OpenAPI/JSON Schema、Pydantic 模型、事件契约、客户端 SDK |
| R02 | ca-ingress | Python/FastAPI | 入口、鉴权缓存、限流、幂等代理 |
| R03 | ca-task | Python/FastAPI | 任务生命周期、审批、质量回显 |
| R04 | ca-design | Python/FastAPI | 设计源与意图解析 |
| R05 | ca-planning | Python/FastAPI | 规划与解耦 |
| R06 | ca-orchestration | Python/FastAPI | DAG、子任务队列、租约 |
| R07 | ca-checkpoint | Python/FastAPI | 检查点、事件、Watchdog |
| R08 | ca-sandbox | Python/FastAPI | 沙箱调度、池、恢复门 |
| R09 | ca-agent-runtime | Python/FastAPI | Agent 循环、上下文装配 |
| R10 | ca-executor | Python/FastAPI | 补丁、工具调用、证据 |
| R11 | ca-tool-gateway | Python/FastAPI | 工具注册、策略、调用 |
| R12 | ca-cache | Python/FastAPI | 缓存键、挂载、失效 |
| R13 | ca-memory | Python/FastAPI | 记忆写入/检索/巩固 |
| R14 | ca-knowledge | Python/FastAPI | 知识入库/检索 |
| R15 | ca-identity | Python/FastAPI | 令牌、鉴权、绑定 |
| R16 | ca-verification | Python/FastAPI | 验证适配、解析、回调 |
| R17 | ca-audit | Python/FastAPI | 审计事件、哈希链 |
| R18 | ca-evolution | Python/FastAPI | 目标、评测、发布 |
| R19 | ca-observability | Python/FastAPI | OTLP 接入、查询、成本 |
| R20 | ca-model-gateway | Python/FastAPI | 模型路由、预算、安全 |
| R21 | ca-deploy | Helm/Terraform/Argo | 环境、命名空间、数据中间件、GitOps |
| R22 | ca-web | TypeScript/Next.js | 人类工作台前端 |

### 3.4 服务依赖与生成顺序

```text
P0 公共底座：ca-contracts → ca-identity → ca-model-gateway
P1 入口与任务：ca-ingress → ca-task → ca-design
P2 规划与编排：ca-planning → ca-orchestration → ca-checkpoint
P3 执行面：ca-sandbox → ca-cache → ca-tool-gateway → ca-agent-runtime → ca-executor
P4 共享与验证：ca-memory → ca-knowledge → ca-verification → ca-audit
P5 横切与交付：ca-observability → ca-evolution → ca-web → ca-deploy
```

> 生成约束  下游服务只能依赖已完成并通过契约测试的上游服务；跨层调用必须经 ca-contracts 中定义的客户端，不允许手写重复 DTO。

## 4 技术栈与版本基线

| 层 | 技术 | 版本 | 用途/约束 |
| --- | --- | --- | --- |
| 语言 | Python | 3.12.x | 后端统一；启用 asyncio、type hints |
| Web | FastAPI + Pydantic | 0.115 / 2.9 | REST/OpenAPI；异步路由 |
| 运行 | Uvicorn + Gunicorn | 0.30 / 22 | 多 worker；优雅停机 |
| ORM/迁移 | SQLAlchemy async + Alembic | 2.0 / 1.13 | asyncpg；迁移向前兼容 |
| 数据库 | PostgreSQL | 16.3 | 服务一库；JSONB 用于扩展字段 |
| 缓存/锁 | Redis Cluster | 7.2 | 幂等、租约、限流、热数据 |
| 消息 | Kafka (KRaft) | 3.7 | 领域事件、Outbox、Trace 缓冲 |
| 分析库 | ClickHouse | 24.3 | Trace/成本/质量事件 |
| 向量库 | Qdrant | 1.11 | 记忆与知识向量检索 |
| 对象存储 | S3/MinIO | — | 检查点、补丁、证据、评测集 |
| 编排 | Kubernetes | 1.30 | 命名空间、NetworkPolicy、gVisor |
| 自动扩缩 | KEDA + HPA | 2.14 / v2 | 按 Kafka 积压与 CPU 扩缩 |
| 网关 | Envoy Gateway | 1.2 | 南北向入口、mTLS 终止 |
| 策略 | OPA | 0.66 | 工具与高风险动作授权 |
| 身份 | SPIRE + Vault | 1.9 / 1.17 | 工作负载身份、密钥 |
| 可观测 | OTel Collector + Prometheus + Grafana + Loki | 0.105 / 2.53 / 11 / 3.0 | 统一 Trace/指标/日志 |
| 发布 | Argo CD + Argo Rollouts | 2.12 / 1.7 | GitOps、灰度与回滚 |
| 测试 | pytest + testcontainers + schemathesis + locust | — | 单测/集成/契约/压测 |
| 质量 | ruff + mypy strict | — | CI 强制 |

## 5 数据与中间件总体规格

### 5.1 集群规格

| 组件 | 拓扑与规格 | 用途 | HA/备份 |
| --- | --- | --- | --- |
| PostgreSQL | 3 节点：1 主 2 备；16C/64GB；NVMe 2TB；PITR 7 天 | 控制面元数据、任务、检查点索引、审计 | 同步 1 副本 + 异步 1；跨 AZ |
| Redis Cluster | 6 节点：3 主 3 从；16GB/节点 | 幂等、租约、限流、热缓存 | AOF everysec；主从切换 |
| Kafka | 3 Broker：8C/32GB；4TB；RF=3，minISR=2 | 领域事件、Outbox、Trace 缓冲 | 7 天保留；分区 24 |
| ClickHouse | 6 节点：3 分片 × 2 副本；32C/128GB；10TB | Trace、成本、质量事件 | 90 天热数据；冷归档 S3 |
| Qdrant | 3 节点：16C/64GB；2TB NVMe | 记忆/知识向量 | RF=2；快照到 S3 |
| S3/MinIO | 多副本对象存储 | 检查点、补丁、证据、缓存、评测集 | 版本化 + 生命周期 |
| Kubernetes | 3 控制面 + 多池工作节点 | 服务与沙箱编排 | 多 AZ；节点自愈 |
| Envoy Gateway | 3 副本 | 入口与 mTLS | 多 AZ |
| OPA/SPIRE/Vault | 3 副本/集群 | 策略、身份、密钥 | HA 模式 |

### 5.2 数据库清单

| 数据库 | 所属服务 | 核心表 | 增长量级 |
| --- | --- | --- | --- |
| ca_task | ca-task | tasks, task_events, approvals, outbox | 任务数 × 20 事件 |
| ca_design | ca-design | design_sources, design_intents, token_snapshots | 设计版本 × 任务 |
| ca_planning | ca-planning | plans, plan_revisions, decouple_reports | 任务 × 1–3 版本 |
| ca_orch | ca-orchestration | dags, dag_nodes, dag_edges, subtasks, subtask_runs | 子任务 × 运行次数 |
| ca_ckpt | ca-checkpoint | checkpoints, checkpoint_events, watchdog_leases | 检查点 × 任务 |
| ca_sandbox | ca-sandbox | pools, sandboxes, leases, mounts | 沙箱 × 生命周期 |
| ca_tool | ca-tool-gateway | tool_registry, tool_versions, policies | 工具数 × 版本 |
| ca_cache | ca-cache | cache_entries, invalidations | 缓存对象数 |
| ca_memory | ca-memory | memory_items, memory_links | 记忆条数 |
| ca_knowledge | ca-knowledge | documents, chunks, index_jobs | 文档 × 分块 |
| ca_identity | ca-identity | subjects, roles, bindings, revocations | 主体 × 绑定 |
| ca_verify | ca-verification | verifications, results, adapters | 验证 × 任务 |
| ca_audit | ca-audit | audit_events（按月分区） | 事件流（大） |
| ca_evolution | ca-evolution | goals, candidates, evaluations, releases | 迭代 × 候选 |
| ca_model | ca-model-gateway | providers, models, budgets | 少量配置 + 预算 |
| ca_obs_meta | ca-observability | saved_views, replay_manifests | 元数据 |

### 5.3 服务与数据库依赖矩阵（摘要）

| 服务 | PostgreSQL | Redis | Kafka | 对象存储 | 向量库 | ClickHouse |
| --- | --- | --- | --- | --- | --- | --- |
| ca-ingress | — | 幂等/限流 | — | — | — | — |
| ca-task | ca_task | 锁/幂等 | task.events | — | — | — |
| ca-design | ca_design | — | design.events | 设计资产 | — | — |
| ca-planning | ca_planning | — | plan.events | — | 知识检索 | — |
| ca-orchestration | ca_orch | 租约 | subtask.events | — | — | — |
| ca-checkpoint | ca_ckpt | 心跳 | checkpoint.events | 检查点 Blob | — | — |
| ca-sandbox | ca_sandbox | 租约 | sandbox.events | — | — | — |
| ca-agent-runtime | — | 会话指针 | — | 临时工作区 | 记忆/知识 | — |
| ca-executor | — | 执行状态 | — | 产物/证据 | — | — |
| ca-tool-gateway | ca_tool | 限流/幂等 | tool.audit | — | — | — |
| ca-cache | ca_cache | 热点元数据 | cache.events | 缓存块 | — | — |
| ca-memory | ca_memory | 热记忆 | memory.events | — | 3 集合 | — |
| ca-knowledge | ca_knowledge | 索引状态 | knowledge.events | 原文 | knowledge_docs | — |
| ca-identity | ca_identity | 令牌缓存 | — | — | — | — |
| ca-verification | ca_verify | 去重 | verify.events | 证据 | — | — |
| ca-audit | ca_audit | — | audit.events | WORM 归档 | — | audit_index |
| ca-evolution | ca_evolution | — | evolution.events | 评测集 | — | 质量/成本 |
| ca-observability | ca_obs_meta | — | trace.spans | Trace 归档 | — | spans/cost |
| ca-model-gateway | ca_model | 限额 | usage.events | — | — | cost 明细 |

## 6 服务通用工程规范

### 6.1 代码仓目录

```text
<repo>/
  pyproject.toml            # 依赖、ruff、mypy、pytest 配置
  src/<package>/
    main.py                 # FastAPI app 工厂 + lifespan
    api/                    # routers、schemas、deps
    domain/                 # 实体、状态机、领域服务
    infra/                  # db、redis、kafka、s3、clients
    workers/                # 消费者、定时任务
    config.py               # Pydantic Settings
  migrations/               # Alembic
  tests/                    # unit / integration / contract
  Dockerfile                # distroless 多阶段构建
  helm/                     # Chart（values per env）
  openapi.yaml              # 由代码生成并校验
```

### 6.2 API 与错误

- 路径 /v1，JSON，字段 snake_case；时间 ISO-8601 UTC；ID 使用 ULID/UUIDv7。
- 分页使用 cursor；列表默认 50、上限 200；幂等键 24h 保留。
- 错误 RFC 7807：type/title/status/detail/instance/trace_id；业务码放 code。
- 写接口校验 If-Match / version；冲突返回 409，重复幂等返回首次结果。

### 6.3 异步与可靠性

- 事务内写业务表 + outbox；独立 publisher 至少一次投递；消费端幂等去重。
- Kafka 消费手动提交；重试按指数退避；超过上限进死信 topic。
- 所有外部调用设置连接/读取超时、重试预算与熔断；禁止无限重试。

### 6.4 可观测与配置

- OTel 自动埋点 + 关键 Span：task、subtask、sandbox、tool、verify、model。
- 结构化 JSON 日志，必带 trace_id、task_id、tenant_id；敏感字段脱敏。
- 暴露 /healthz、/readyz、/metrics；SLO 指标统一命名 ca_<service>_<metric>。
- 配置 Pydantic Settings，secret 由 Vault/CSI 注入，不落代码与镜像。

### 6.5 测试与交付门禁

- 单元测试覆盖领域逻辑 ≥80%；集成测试用 testcontainers 起 PG/Redis/Kafka。
- 契约测试：服务实现与 ca-contracts 的 OpenAPI/JSON Schema 双向校验。
- 压测基线：每个服务给出 P95/P99 与最大并发；超限需降级而非雪崩。
- CI：ruff + mypy + unit + integration + contract + image scan + SBOM。
- CD：Argo CD 同步，Argo Rollouts 灰度；失败自动回滚。

## 7 服务详细设计

每个服务章节包含可独立用于代码生成的完整规格：元数据、职责边界、API、存储与依赖、关键技术设计、类职责、配置、失败处理与验收标准。服务编号与第 3 章一致。

### 7.1 ca-ingress（ca-ingress）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S01 / 控制面 |
| 代码仓 / 端口 | ca-ingress / 8001 |
| 副本 / 资源 | 3–12；0.5C/512Mi 请求，2C/1Gi 上限 |
| SLO | P95 30ms；可用性 99.95% |
| 生成顺序 | P1 |

所有外部请求的唯一入口：TLS 终止后的身份校验、租户识别、限流、幂等、审计上下文注入与请求代理。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不实现任务业务逻辑，不直接访问业务库；不承载长连接任务执行。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/tasks | 代理创建任务；校验 JWT、幂等键、租户配额 |
| GET | /v1/tasks/{id} | 查询任务摘要；合并 task/quality 视图 |
| POST | /v1/design-intents | 代理设计意图提交 |
| GET | /v1/quality/{task_id} | 聚合质量看板摘要 |
| POST | /v1/approvals/{id}:decide | 人工审批代理 |
| GET | /healthz\|/readyz\|/metrics | 健康与指标 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | Redis：ca:idem:{key}（24h）、ca:rl:{tenant}:{route}（令牌桶）、ca:auth:{token}（30s 缓存）；无业务数据库 |
| 依赖服务/中间件 | ca-identity（introspect/authorize）；ca-task；ca-design；Redis；Envoy Gateway |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 令牌桶用 Redis Lua 原子执行，按租户/路由/用户三级配额。
- JWT 内省结果缓存 30s，吊销列表本地布隆过滤 + 定期同步。
- 幂等过滤在入口完成，重复请求直接返回首次响应快照。
- 为每个请求生成 trace_id/request_id 并透传，禁止下游重复生成。
| 类/模块 | 职责 |
| --- | --- |
| IngressApp | 应用工厂与 lifespan |
| AuthMiddleware | JWT 校验与租户解析 |
| RateLimiter | Redis 令牌桶 |
| IdempotencyFilter | 幂等键与响应快照 |
| ProxyRouter | 到下游服务的转发与超时 |

```text
ca-ingress/
  src/ca_ingress/
    main.py  config.py
    api/       # /v1/tasks 等路由
    domain/    # IngressApp, AuthMiddleware, RateLimiter
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| IDENTITY_URL | http://ca-identity:8000 |
| REDIS_URL | redis://... |
| RATE_LIMIT_RPS | 100/tenant |
| IDEMPOTENCY_TTL_H | 24 |
| UPSTREAM_TIMEOUT_MS | 3000 |

#### 失败处理与验收

- 失败与降级：身份服务不可用：拒绝新请求（fail-closed），健康检查失败并告警。；Redis 不可用：限流降级为本地令牌桶，幂等降级为单实例内存并标记风险。
- 验收：未认证请求 401，越权 403，超限 429，重复请求返回同一结果。
- 验收：P95 代理开销 ≤30ms；trace_id 全链路一致。

### 7.2 ca-task（ca-task）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S02 / 控制面 |
| 代码仓 / 端口 | ca-task / 8002 |
| 副本 / 资源 | 2–8；1C/1Gi 请求，2C/2Gi 上限 |
| SLO | P95 100ms；状态迁移成功率 99.9% |
| 生成顺序 | P1 |

任务生命周期与人工工作流主服务：受理、状态机、暂停/恢复/分叉编排、审批与质量回显。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不执行规划/编码；不直接调度沙箱；不保存检查点内容（只引用）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/tasks | 创建任务（幂等）；返回 task_id/state |
| GET | /v1/tasks/{id} | 任务详情、当前 DAG、最近事件 |
| POST | /v1/tasks/{id}:pause\|resume\|fork | 调用 checkpoint 并驱动状态迁移 |
| POST | /v1/tasks/{id}/approvals | 创建/查询人工审批 |
| POST | /v1/tasks/{id}/quality | 回写质量摘要（内部） |
| GET | /v1/tasks | 按租户/状态/时间分页 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_task：tasks、task_events、approvals、outbox；Redis：任务锁 ca:lock:task:{id}（30s 续约） |
| 依赖服务/中间件 | ca-identity；ca-planning；ca-orchestration；ca-checkpoint；Kafka；PostgreSQL；Redis |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 状态机以显式迁移表实现，非法迁移返回 409；每次迁移写事件与 outbox。
- 乐观锁 version + 条件更新，避免并发状态覆盖。
- 暂停/恢复为 Saga：先请求 checkpoint，成功后迁移；失败补偿并告警。
- 审批包保存证据引用与 TTL，超时按策略自动升级/拒绝。
| 类/模块 | 职责 |
| --- | --- |
| TaskService | 任务用例编排 |
| TaskStateMachine | 合法迁移与守卫 |
| TaskRepository | 聚合持久化 |
| OutboxPublisher | 事务性事件发布 |
| ApprovalService | 人工审批与超时 |
| QualityEchoService | 质量摘要回写 |

```text
ca-task/
  src/ca_task/
    main.py  config.py
    api/       # /v1/tasks 等路由
    domain/    # TaskService, TaskStateMachine, TaskRepository
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| MAX_ACTIVE_TASKS_PER_TENANT | 200 |
| APPROVAL_TTL_H | 24 |
| PAUSE_TIMEOUT_MS | 5000 |
| OUTBOX_BATCH | 100 |

#### 失败处理与验收

- 失败与降级：检查点服务不可用：暂停/恢复返回 503，任务保持原状态，不半迁移。；Kafka 不可用：业务提交仍成功，outbox 积压重试并告警。
- 验收：所有状态迁移可回放；重复 pause/resume 幂等；审批超时策略可验证。

### 7.3 ca-design（ca-design）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S03 / 控制面 |
| 代码仓 / 端口 | ca-design / 8003 |
| 副本 / 资源 | 2–6；0.5C/1Gi 请求，2C/2Gi 上限 |
| SLO | P95 3s；解析失败可重试 |
| 生成顺序 | P1 |

设计源接入与设计意图解析：把设计稿、Token、组件库与工单转成 Agent 可执行约束、验收条件与置信度。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不生成代码；不执行验证；不做任务调度。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/design-intents | 解析并保存设计意图 |
| GET | /v1/design-intents/{id} | 查询意图与置信度 |
| POST | /v1/design-sources/sync | 同步 Figma/工单变更 |
| GET | /v1/design-sources/{id} | 版本与快照 |
| GET | /v1/tokens/{version} | 获取 Token 快照 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_design：design_sources、design_intents、token_snapshots；S3：design-assets/{tenant}/{source}/{version}；Kafka：design.events |
| 依赖服务/中间件 | ca-model-gateway（意图抽取）；ca-knowledge（组件规范）；S3；Kafka |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 适配器模式接入 Figma/工单，统一为 DesignSource 版本快照。
- 意图流水线：规范化 → Token 映射 → 组件映射 → 约束生成 → 验收生成 → 置信度。
- 置信度低于阈值时输出 clarification 列表，不静默降级。
- 全部输入带 source_refs，保证可追溯与增量重解析。
| 类/模块 | 职责 |
| --- | --- |
| DesignSourceAdapter | 源系统适配与增量同步 |
| TokenResolver | 语义 Token 与版本 |
| IntentParser | 意图与约束抽取 |
| ConfidenceScorer | 置信度与澄清项 |
| IntentRepository | 版本化持久化 |

```text
ca-design/
  src/ca_design/
    main.py  config.py
    api/       # /v1/design-intents 等路由
    domain/    # DesignSourceAdapter, TokenResolver, IntentParser
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| MODEL_TIMEOUT_MS | 15000 |
| MIN_CONFIDENCE | 0.75 |
| MAX_TOKENS_PER_INTENT | 12000 |
| S3_BUCKET | design-assets |

#### 失败处理与验收

- 失败与降级：模型网关不可用：返回 503 并保留源快照，允许稍后重试。；设计源版本冲突：拒绝覆盖，生成新版本并标记冲突。
- 验收：同一 source 版本重复解析结果一致；低置信度必带澄清项；约束可被 planner 直接消费。

### 7.4 ca-planning（ca-planning）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S04 / 控制面 |
| 代码仓 / 端口 | ca-planning / 8004 |
| 副本 / 资源 | 2–6；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | P95 5s；计划校验 100% 通过 |
| 生成顺序 | P2 |

任务理解、解耦分析与计划生成：把目标与设计意图转成可执行子任务草案、验收策略与风险分级。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不执行 DAG 调度；不写检查点；不直接调用工具。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/plans | 生成计划；入参 task_id/design_intent |
| GET | /v1/plans/{id} | 计划、假设、风险 |
| POST | /v1/plans/{id}/replan | 基于验证/设计变更重规划 |
| POST | /v1/decouple/analyze | 独立解耦分析 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_planning：plans、plan_revisions、decouple_reports |
| 依赖服务/中间件 | ca-design；ca-memory；ca-knowledge；ca-model-gateway；ca-orchestration |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 计划使用结构化 PlanSchema：goal/assumptions/steps/acceptance/risks/global_tasks。
- 解耦算法结合依赖图聚类与启发式评分（耦合度、变更半径、契约、覆盖、环境）。
- 不可解耦任务标记 global_lock，交编排器串行协调。
- 重规划保留历史版本与差异，禁止覆盖已执行计划。
| 类/模块 | 职责 |
| --- | --- |
| Planner | 计划生成与校验 |
| ContextBuilder | 设计/记忆/知识装配 |
| DecoupleAnalyzer | 依赖图与评分 |
| RiskScorer | 风险与门禁建议 |
| PlanValidator | Schema 与一致性校验 |

```text
ca-planning/
  src/ca_planning/
    main.py  config.py
    api/       # /v1/plans 等路由
    domain/    # Planner, ContextBuilder, DecoupleAnalyzer
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| MAX_PLAN_STEPS | 50 |
| MODEL_TIMEOUT_MS | 30000 |
| MAX_REPLAN | 3 |
| GLOBAL_LOCK_TIMEOUT_S | 600 |

#### 失败处理与验收

- 失败与降级：模型超时：返回可重试错误，不生成半计划。；计划校验失败：拒绝提交并返回具体字段错误。
- 验收：计划可被 orchestration 直接构建 DAG；解耦报告可解释；重规划保留版本。

### 7.5 ca-orchestration（ca-orchestration）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S05 / 控制面 |
| 代码仓 / 端口 | ca-orchestration / 8005 |
| 副本 / 资源 | 3–12；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | P95 200ms；调度不丢任务 |
| 生成顺序 | P2 |

全局 DAG 编排与子任务队列：依赖推进、关键路径、租约下发、完成/失败处理与重规划。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不执行代码；不管理沙箱生命周期；不做验证解析。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/dags | 由计划构建 DAG |
| POST | /v1/dags/{id}/advance | 推进就绪节点（内部/定时） |
| GET | /v1/dags/{id} | DAG 状态与关键路径 |
| POST | /v1/subtasks/{id}/complete\|fail | 子任务结果回传 |
| POST | /v1/leases/acquire | 获取/续约子任务租约 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_orch：dags、dag_nodes、dag_edges、subtasks、subtask_runs、outbox；Redis：租约 ca:lease:{subtask}（TTL 60s，续约） |
| 依赖服务/中间件 | ca-task；ca-checkpoint；ca-sandbox；PostgreSQL；Redis；Kafka |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- Kahn 拓扑 + 优先队列：priority=w1*critical_path+w2*age+w3*risk。
- 租约下发，超时回收；完成回调幂等（subtask_id+attempt）。
- 全局锁任务占用互斥通道；失败触发局部重规划而非全图重跑。
- 积压监控与背压：按租户限流，避免单租户饿死其他租户。
| 类/模块 | 职责 |
| --- | --- |
| DagBuilder | 计划到图 |
| DagScheduler | 就绪集合与优先级 |
| SubtaskQueue | 分区队列与公平性 |
| LeaseManager | 租约与回收 |
| CompletionHandler | 幂等完成与重规划 |

```text
ca-orchestration/
  src/ca_orchestration/
    main.py  config.py
    api/       # /v1/dags 等路由
    domain/    # DagBuilder, DagScheduler, SubtaskQueue
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| LEASE_TTL_S | 60 |
| MAX_PARALLEL_PER_TENANT | 50 |
| SCHEDULER_TICK_MS | 500 |
| DEAD_LETTER_RETRY | 3 |

#### 失败处理与验收

- 失败与降级：沙箱不可用：停止下发，保留就绪集合，恢复后续跑。；完成重复上报：幂等忽略并记录审计。
- 验收：DAG 无环校验；依赖满足才下发；租约超时自动回收；关键路径可查询。

### 7.6 ca-checkpoint（ca-checkpoint）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S06 / 控制面 |
| 代码仓 / 端口 | ca-checkpoint / 8006 |
| 副本 / 资源 | 3–9；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | 写 P95 200ms；恢复 P95 60s |
| 生成顺序 | P2 |

检查点与续存管理：任务身份、检查点生成/恢复/分叉、Stored Work 事件、Watchdog 监督。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不保存工作树的实时内容（存 S3 引用）；不执行恢复门校验（由 sandbox 执行）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/checkpoints | 创建检查点；返回 snapshot_ref |
| GET | /v1/checkpoints/{id} | 查询元数据与校验和 |
| POST | /v1/checkpoints/{id}:recover\|fork | 发起恢复/分叉 |
| POST | /v1/watchdog/heartbeat | 任务进展心跳 |
| GET | /v1/watchdog/{task_id} | 监督状态与告警 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_ckpt：checkpoints、checkpoint_events、watchdog_leases、outbox；S3：checkpoints/{tenant}/{task}/{ckpt}/（manifest+blobs）；Redis：恢复锁 |
| 依赖服务/中间件 | S3；Kafka；ca-sandbox（恢复门）；PostgreSQL；Redis |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 元数据与 Blob 分离：PG 存索引/校验和，S3 存增量快照。
- 事件先写（Stored Work + outbox），事务提交后再触发外部动作。
- Watchdog 独立循环：存活（心跳）+ 实质进展（步数/产物/时间）双指标。
- 保留策略按任务等级；恢复/分叉写不可变审计事件。
| 类/模块 | 职责 |
| --- | --- |
| CheckpointService | 创建/恢复/分叉 |
| SnapshotStore | S3 分块与校验 |
| EventLog | 事件序列与重放 |
| StoredWorkOutbox | 副作用意图 |
| Watchdog | 双指标监督 |
| RecoveryCoordinator | 调用恢复门 |

```text
ca-checkpoint/
  src/ca_checkpoint/
    main.py  config.py
    api/       # /v1/checkpoints 等路由
    domain/    # CheckpointService, SnapshotStore, EventLog
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| SNAPSHOT_CHUNK_MB | 8 |
| CHECKPOINT_RETENTION_D | 30 |
| WATCHDOG_INTERVAL_S | 30 |
| PROGRESS_STALL_S | 600 |

#### 失败处理与验收

- 失败与降级：S3 不可用：检查点创建失败但任务可继续，标记不可恢复风险。；恢复门失败：回退上一检查点或升级人工。
- 验收：检查点可恢复且校验和一致；重复恢复幂等；Watchdog 可发现停滞任务。

### 7.7 ca-sandbox（ca-sandbox）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S07 / 执行面 |
| 代码仓 / 端口 | ca-sandbox / 8007 |
| 副本 / 资源 | 2–6；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | 调度 P95 50ms；热启动 30s |
| 生成顺序 | P3 |

两层调度的下层：沙箱池与并发实例管理、资源装箱、缓存挂载、恢复门校验与隔离执行环境。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不运行 Agent 推理；不执行具体工具；不保存业务状态。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/dispatch | 为子任务分配沙箱（含缓存挂载） |
| POST | /v1/sandboxes | 创建/复用沙箱；返回 sandbox_id |
| DELETE | /v1/sandboxes/{id} | 回收与清理 |
| POST | /v1/sandboxes/{id}/recover | 恢复门校验并重建工作树 |
| GET | /v1/pools | 池容量与命中率 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_sandbox：pools、sandboxes、leases、mounts；Redis：租约/心跳；K8s：Pod/Namespace/NetworkPolicy |
| 依赖服务/中间件 | K8s API；ca-cache；ca-tool-gateway；ca-agent-runtime；ca-checkpoint |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 装箱评分：score=w1*cache_affinity+w2*resource_fit−w3*cold_start−w4*fragmentation。
- 热池按语言/框架/工具域预热；冷池按需创建；复用前彻底清理。
- gVisor runtimeClass + 独立命名空间 + 默认拒绝网络策略。
- 恢复门顺序：锚点→代码位置→依赖→外部副作用→权限→检查点完整性。
- 回收失败进入隔离区，人工确认后再复用。
| 类/模块 | 职责 |
| --- | --- |
| SandboxScheduler | 装箱与亲和 |
| PoolManager | 热池/冷池与复用 |
| K8sProvisioner | Pod/网络/配额 |
| CacheMounter | COW 挂载 |
| RecoveryGate | 漂移校验 |
| SandboxReaper | 清理与隔离 |

```text
ca-sandbox/
  src/ca_sandbox/
    main.py  config.py
    api/       # /v1/dispatch 等路由
    domain/    # SandboxScheduler, PoolManager, K8sProvisioner
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| WARM_POOL_SIZE | 20 |
| SANDBOX_CPU | 4 |
| SANDBOX_MEM_GI | 8 |
| MAX_SANDBOXES | 500 |
| RUNTIME_CLASS | gvisor |

#### 失败处理与验收

- 失败与降级：K8s 资源不足：排队并回压队列；不允许超卖。；恢复门失败：隔离沙箱并回退检查点。
- 验收：沙箱启动达标；缓存挂载一致；恢复门六项校验全部执行并留痕。

### 7.8 ca-agent-runtime（ca-agent-runtime）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S08 / 执行面 |
| 代码仓 / 端口 | ca-agent-runtime / 8008 |
| 副本 / 资源 | 10–200；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 单步 P95 8s（模型相关） |
| 生成顺序 | P3 |

无状态 Agent Runtime（脑）：装配上下文、执行 plan-execute 循环、生成工具调用与执行指令。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不保存会话状态；不直接执行 shell；不持有长期凭证。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/sessions | 创建会话（task/subtask/context ref） |
| POST | /v1/sessions/{id}/step | 执行一步：推理+工具选择 |
| POST | /v1/sessions/{id}/cancel | 取消与预算终止 |
| GET | /v1/sessions/{id} | 当前步摘要（不保证持久化） |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | 无数据库；Redis 仅存会话指针/取消信号；S3 临时工作区（短 TTL） |
| 依赖服务/中间件 | ca-model-gateway；ca-memory；ca-knowledge；ca-tool-gateway；ca-executor；OTel |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- Plan-Execute 循环：读取计划→选择下一步→调用工具/执行器→观察→更新计划。
- 上下文装配分层：系统约束→任务→设计→记忆/知识→最近结果，按预算裁剪。
- 预算守卫：步数、Token、时长、工具次数；超限优雅停止并上报。
- 无状态：每步可从检查点恢复；不依赖进程内历史。
| 类/模块 | 职责 |
| --- | --- |
| AgentSession | 会话上下文与生命周期 |
| ContextAssembler | 分层上下文与裁剪 |
| PlanExecutor | 步骤执行与观察 |
| ToolSelector | 工具选择与参数 |
| BudgetGuard | 预算与停止 |
| StepReporter | 事件与 Trace |

```text
ca-agent-runtime/
  src/ca_agent_runtime/
    main.py  config.py
    api/       # /v1/sessions 等路由
    domain/    # AgentSession, ContextAssembler, PlanExecutor
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| MAX_STEPS | 40 |
| MAX_TOKENS | 200000 |
| STEP_TIMEOUT_S | 120 |
| MODEL_GATEWAY_URL | http://ca-model-gateway:8000 |

#### 失败处理与验收

- 失败与降级：模型限流：退避/降级模型；仍失败则暂停并请求检查点。；工具不可用：记录并尝试替代路径，禁止臆造结果。
- 验收：同一会话可在新 Pod 恢复；预算超限必停止；每步产出可追溯 Trace。

### 7.9 ca-executor（ca-executor）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S09 / 执行面 |
| 代码仓 / 端口 | ca-executor / 8009 |
| 副本 / 资源 | 5–100；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | 执行开销 P95 1s（不含命令） |
| 生成顺序 | P3 |

子任务执行与装配：代码检索/编辑、补丁生成、命令与测试触发、证据收集与结果上报。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不做任务规划与全局调度；不直接访问外部系统（经工具网关）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/executions | 执行子任务；返回 execution_id |
| POST | /v1/executions/{id}/patch | 生成/应用补丁 |
| POST | /v1/executions/{id}/test | 触发测试并收集证据 |
| GET | /v1/executions/{id}/evidence | 证据清单与引用 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | 无业务库；S3 artifacts/{tenant}/{task}/{subtask}；Redis 执行状态（TTL 2h） |
| 依赖服务/中间件 | ca-sandbox；ca-tool-gateway；ca-checkpoint；ca-cache；S3 |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- Worktree 管理：独立 worktree + base commit 校验 + 未提交变更保护。
- 补丁以 unified diff 生成，应用前做冲突检测与最小化。
- 工具调用经网关，写操作带幂等键；外部副作用先登记 Stored Work。
- 证据：命令、退出码、日志摘要、测试报告、覆盖率与截图引用。
| 类/模块 | 职责 |
| --- | --- |
| ExecutionService | 执行用例编排 |
| WorktreeManager | 工作树与分支 |
| PatchBuilder | diff 生成/应用 |
| ToolCallLoop | 工具调用与观察 |
| EvidenceCollector | 证据归档 |
| TestRunner | 测试触发与解析 |

```text
ca-executor/
  src/ca_executor/
    main.py  config.py
    api/       # /v1/executions 等路由
    domain/    # ExecutionService, WorktreeManager, PatchBuilder
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| WORKSPACE_ROOT | /workspace |
| MAX_PATCH_FILES | 50 |
| CMD_TIMEOUT_S | 600 |
| EVIDENCE_BUCKET | artifacts |

#### 失败处理与验收

- 失败与降级：补丁冲突：回退并重新规划，不强行覆盖。；命令超时：终止进程树并记录证据。
- 验收：补丁可重复应用；证据完整可回溯；所有外部写操作可审计。

### 7.10 ca-tool-gateway（ca-tool-gateway）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S10 / 执行面 |
| 代码仓 / 端口 | ca-tool-gateway / 8010 |
| 副本 / 资源 | 3–20；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | 网关开销 P95 20ms |
| 生成顺序 | P3 |

有状态“手”：工具注册/版本、部门工具域隔离、鉴权、参数校验、限流、幂等、执行与审计。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不实现具体业务工具逻辑；不保存代码/记忆业务数据。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/tools/{tool}/invoke | 调用工具；幂等键必填（写操作） |
| GET | /v1/tools | 工具清单与域 |
| POST | /v1/tools/register | 注册/升级工具版本（管理员） |
| GET | /v1/tools/{tool}/schema | 参数与返回 Schema |
| POST | /v1/policies/evaluate | 内部策略评估 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_tool：tool_registry、tool_versions、policies；Redis：ca:tool:rl、ca:tool:idem |
| 依赖服务/中间件 | ca-identity；OPA；Vault/KMS；ca-audit；PostgreSQL；Redis；Kafka |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 调用链：身份→策略(OPA)→Schema→限流→幂等→执行→审计。
- 工具域按部门隔离；跨域调用需显式授权；高危工具双人复核。
- 工具运行于受限环境：只读根、无宿主、网络白名单、超时与输出上限。
- 写操作幂等键 + 结果缓存；失败按可重试性分类。
| 类/模块 | 职责 |
| --- | --- |
| ToolGateway | 调用编排 |
| ToolRegistry | 注册与版本 |
| PolicyEnforcer | OPA 决策 |
| RateLimiter | 租户/工具限流 |
| ToolExecutor | 隔离执行 |
| AuditEmitter | 审计事件 |

```text
ca-tool-gateway/
  src/ca_tool_gateway/
    main.py  config.py
    api/       # /v1/tools/{tool}/invoke 等路由
    domain/    # ToolGateway, ToolRegistry, PolicyEnforcer
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| OPA_URL | http://opa:8181 |
| TOOL_TIMEOUT_S | 120 |
| MAX_OUTPUT_BYTES | 1048576 |
| IDEM_TTL_H | 24 |

#### 失败处理与验收

- 失败与降级：OPA 不可用：高危工具 fail-closed，低危只读可降级。；工具超时：终止并返回可重试错误，不重放写操作。
- 验收：越权调用被拒绝；Schema 校验生效；每次调用有审计与 Trace。

### 7.11 ca-cache（ca-cache）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S11 / 数据面 |
| 代码仓 / 端口 | ca-cache / 8011 |
| 副本 / 资源 | 3–12；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 挂载 P95 500ms |
| 生成顺序 | P3 |

代码与依赖缓存的挂载接口：内容寻址、COW 挂载、增量更新、签名校验、失效与配额。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不执行构建；不保存业务数据；不做验证。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/mounts | 按 repo/commit/deps 挂载；返回 mount_id |
| DELETE | /v1/mounts/{id} | 卸载与清理 |
| POST | /v1/invalidate | 按 key/仓库失效 |
| GET | /v1/cache/entries/{key} | 查询元数据与命中 |
| POST | /v1/cache/warm | 预热常用仓库 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_cache：cache_entries、invalidations；S3：cache/{kind}/{key}/（内容寻址）；Redis：热点元数据/命中计数 |
| 依赖服务/中间件 | S3；代码仓/制品库；ca-sandbox（挂载目标）；Redis；PostgreSQL |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- Key=hash(repo_url+commit+deps_lock+toolchain)；内容寻址保证可复用。
- COW：只读基础层 + 沙箱可写层；卸载丢弃写层。
- 两级缓存：本地节点热块 + S3 冷块；按访问频率预取。
- 签名与租户命名空间隔离；失效采用主动+TTL+回源校验。
| 类/模块 | 职责 |
| --- | --- |
| CacheKeyResolver | 缓存键计算 |
| ContentStore | S3 分块存取 |
| MountManager | COW 挂载 |
| CacheValidator | 签名与一致性 |
| Invalidator | 主动/被动失效 |

```text
ca-cache/
  src/ca_cache/
    main.py  config.py
    api/       # /v1/mounts 等路由
    domain/    # CacheKeyResolver, ContentStore, MountManager
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| CACHE_BUCKET | cache |
| MAX_CACHE_GB | 2048 |
| MOUNT_TIMEOUT_MS | 3000 |
| SIGNING_KEY_ID | cache-v1 |

#### 失败处理与验收

- 失败与降级：S3 不可用：回源拉取并降级为无缓存，标记慢路径。；签名失败：拒绝挂载并告警。
- 验收：相同 key 命中率≥90%；挂载后版本一致；跨租户不可见。

### 7.12 ca-memory（ca-memory）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S12 / 共享 |
| 代码仓 / 端口 | ca-memory / 8012 |
| 副本 / 资源 | 3–12；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 检索 P95 80ms |
| 生成顺序 | P4 |

共享记忆：短期任务记忆、长期经验记忆、向量检索、权限隔离、巩固与遗忘。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不存原始代码仓；不做知识文档入库（交 knowledge）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/memories | 写入记忆（来源/置信度/TTL） |
| POST | /v1/memories/retrieve | 混合检索（向量+关键词+时效） |
| DELETE | /v1/memories/{id} | 删除/失效 |
| POST | /v1/memories/consolidate | 巩固与去重 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_memory：memory_items、memory_links；Qdrant：memory_short、memory_long；Redis：会话热记忆（TTL） |
| 依赖服务/中间件 | Qdrant；PostgreSQL；Redis；ca-model-gateway（embedding）；ca-identity；ca-audit |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 分层：短期（任务/TTL）/长期（项目/经验）；写入带来源与置信度。
- 混合检索：score=0.6*cos+0.3*bm25+0.1*recency，先 ACL 过滤。
- 巩固：相似去重、冲突标记、过期归档；低置信不升级为长期。
- 防投毒：写入审计、异常检测、可回滚版本。
| 类/模块 | 职责 |
| --- | --- |
| MemoryService | 写入/检索用例 |
| MemoryRepository | PG 元数据 |
| Embedder | 批量向量化 |
| HybridRetriever | 混合排序 |
| Consolidator | 去重与巩固 |
| MemoryPolicy | ACL/TTL/置信度 |

```text
ca-memory/
  src/ca_memory/
    main.py  config.py
    api/       # /v1/memories 等路由
    domain/    # MemoryService, MemoryRepository, Embedder
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| SHORT_TTL_H | 72 |
| TOP_K | 10 |
| EMBED_BATCH | 64 |
| MIN_WRITE_CONF | 0.5 |

#### 失败处理与验收

- 失败与降级：Qdrant 不可用：降级为关键词/PG 检索并标注质量。；embedding 失败：暂存待重试，不写入半向量。
- 验收：检索结果带引用与置信度；跨租户不可见；删除可验证。

### 7.13 ca-knowledge（ca-knowledge）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S13 / 共享 |
| 代码仓 / 端口 | ca-knowledge / 8013 |
| 副本 / 资源 | 2–8；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 检索 P95 120ms |
| 生成顺序 | P4 |

企业私域知识库：文档/代码入库、切分、索引、检索 API、ACL 过滤、增量与新鲜度。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不做会话短期记忆；不执行代码。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/documents | 上传/登记文档与代码路径 |
| POST | /v1/documents/{id}/reindex | 重建索引 |
| POST | /v1/search | 检索（带 ACL 与引用） |
| DELETE | /v1/documents/{id} | 删除与索引清理 |
| GET | /v1/documents/{id} | 版本/状态 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_knowledge：documents、chunks、index_jobs；Qdrant：knowledge_docs；S3：knowledge/{tenant}/{doc}/原文 |
| 依赖服务/中间件 | S3；Qdrant；ca-model-gateway（embedding）；ca-identity；代码仓 |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 入库流水线：拉取→解析→切分→向量化→索引→ACL 标注→新鲜度。
- 代码按仓/分支/commit 建索引，禁止跨版本污染。
- 检索先权限过滤再排序，返回引用/版本/新鲜度。
- 增量索引与失败重试；删除需同步清理向量与缓存。
| 类/模块 | 职责 |
| --- | --- |
| IngestionPipeline | 入库编排 |
| DocumentParser | 格式解析 |
| Chunker | 结构化切分 |
| Indexer | 向量与倒排 |
| KnowledgeSearch | 检索与引用 |
| ACLFilter | 权限过滤 |

```text
ca-knowledge/
  src/ca_knowledge/
    main.py  config.py
    api/       # /v1/documents 等路由
    domain/    # IngestionPipeline, DocumentParser, Chunker
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| CHUNK_SIZE | 800 |
| CHUNK_OVERLAP | 120 |
| INDEX_BATCH | 32 |
| MAX_DOC_MB | 50 |

#### 失败处理与验收

- 失败与降级：索引失败：文档标记 failed 并保留原文，可重试。；Qdrant 不可用：降级关键词检索。
- 验收：检索命中带引用；删除后不可检索；代码索引版本正确。

### 7.14 ca-identity（ca-identity）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S14 / 共享 |
| 代码仓 / 端口 | ca-identity / 8014 |
| 副本 / 资源 | 3–6；0.5C/512Mi 请求，1C/1Gi 上限 |
| SLO | P95 10ms |
| 生成顺序 | P0 |

Agent 与服务的可验证身份：工作负载身份、用户委托、短期令牌、细粒度授权、密钥轮换。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不承载业务权限页面；不存业务数据。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/token | 签发短期访问令牌 |
| POST | /v1/introspect | 校验令牌与返回声明 |
| POST | /v1/authorize | 资源/动作授权决策 |
| GET | /.well-known/jwks.json | 公钥 |
| POST | /v1/revoke | 吊销令牌/主体 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_identity：subjects、roles、bindings、revocations；Redis：令牌/决策缓存（短 TTL） |
| 依赖服务/中间件 | SPIRE；Vault/KMS；PostgreSQL；Redis；OPA |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 工作负载身份（SPIFFE）+ 用户委托链；权限取交集。
- JWT 短 TTL（≤15min）+ 吊销列表；密钥定期轮换。
- 授权模型 RBAC+ABAC：租户/部门/仓库/环境/风险条件。
- 所有签发/吊销/拒绝事件写审计。
| 类/模块 | 职责 |
| --- | --- |
| TokenIssuer | 签发与签名 |
| Introspector | 校验与缓存 |
| Authorizer | RBAC/ABAC 决策 |
| BindingStore | 角色与绑定 |
| KeyManager | KMS 轮换 |
| RevocationService | 吊销列表 |

```text
ca-identity/
  src/ca_identity/
    main.py  config.py
    api/       # /v1/token 等路由
    domain/    # TokenIssuer, Introspector, Authorizer
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| TOKEN_TTL_MIN | 15 |
| JWKS_ROTATE_D | 7 |
| DECISION_CACHE_S | 30 |
| SPIRE_SOCKET | /run/spire/sockets/agent.sock |

#### 失败处理与验收

- 失败与降级：KMS 不可用：停止签发（fail-closed）。；缓存不一致：以权威库为准并刷新。
- 验收：过期/吊销令牌被拒绝；越权决策可解释；密钥轮换无中断。

### 7.15 ca-verification（ca-verification）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S15 / 验证 |
| 代码仓 / 端口 | ca-verification / 8015 |
| 副本 / 资源 | 2–8；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | 提交 P95 100ms；结果回传 P95 5min |
| 生成顺序 | P4 |

验证反馈网关与结构化解析：统一接入测试/CI/扫描/性能/验收，输出 VerifyResult 与决策建议。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不执行测试（由外部系统/沙箱执行）；不做代码修复。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/verifications | 提交验证请求 |
| POST | /v1/callbacks/{adapter} | 接收结果回调（签名校验） |
| GET | /v1/verifications/{id} | 结果与证据 |
| POST | /v1/verifications/{id}/retry | 重试验证 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_verify：verifications、results、adapters；S3：verify-evidence/{task}/{verify}；Kafka：verify.events |
| 依赖服务/中间件 | CI/测试/扫描系统；Kafka；PostgreSQL；S3；ca-model-gateway（根因辅助） |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 适配器注册：不同系统协议→统一事件；回调签名与去重。
- 结果标准化：status/failure_class/root_cause/evidence/risk/confidence。
- 根因规则优先（堆栈/变更关联），模型仅辅助并标注置信度。
- 失败分类驱动重试或人工；环境/权限错误不重试业务逻辑。
| 类/模块 | 职责 |
| --- | --- |
| VerificationGateway | 提交与状态 |
| AdapterRegistry | 系统适配 |
| ResultNormalizer | 标准化 |
| RootCauseClassifier | 根因线索 |
| CallbackVerifier | 签名与去重 |

```text
ca-verification/
  src/ca_verification/
    main.py  config.py
    api/       # /v1/verifications 等路由
    domain/    # VerificationGateway, AdapterRegistry, ResultNormalizer
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| CALLBACK_SECRET_ROTATE_D | 30 |
| VERIFY_TIMEOUT_S | 1800 |
| MAX_EVIDENCE_MB | 100 |
| DEDUP_TTL_H | 24 |

#### 失败处理与验收

- 失败与降级：外部系统不可用：标记 error 并重试；不伪造通过。；回调签名失败：拒绝并告警。
- 验收：结果结构完整；重复回调幂等；证据可追溯。

### 7.16 ca-audit（ca-audit）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S16 / 共享 |
| 代码仓 / 端口 | ca-audit / 8016 |
| 副本 / 资源 | 3–9；1C/2Gi 请求，2C/4Gi 上限 |
| SLO | 写入 P95 50ms |
| 生成顺序 | P4 |

审计与合规：记录任务、权限、工具、检查点恢复、人工审批与发布事件，提供不可篡改查询与导出。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不做业务逻辑；不存储完整敏感载荷（只存摘要/引用）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/events | 写入审计事件（内部） |
| GET | /v1/events | 按主体/对象/时间检索 |
| GET | /v1/events/{id} | 事件详情 |
| POST | /v1/export | 合规导出（签名） |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_audit：audit_events（按月分区）；S3：audit-archive/（WORM）；ClickHouse：audit_index（检索） |
| 依赖服务/中间件 | Kafka；PostgreSQL；S3；KMS；ClickHouse |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 追加写 + 哈希链（prev_hash+payload_hash）保证不可篡改。
- PII 脱敏，原文引用 S3 受限路径；保留期按合规配置。
- 异步索引到 ClickHouse，PG 为权威源。
- 导出带签名与访问审计。
| 类/模块 | 职责 |
| --- | --- |
| AuditIngestor | 事件接收 |
| HashChain | 链式摘要 |
| EventStore | 分区持久化 |
| Redactor | 脱敏 |
| ExportService | 签名导出 |

```text
ca-audit/
  src/ca_audit/
    main.py  config.py
    api/       # /v1/events 等路由
    domain/    # AuditIngestor, HashChain, EventStore
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| RETENTION_DAYS | 365 |
| PARTITION_MONTHS | 1 |
| ARCHIVE_BUCKET | audit-archive |
| HASH_ALGO | sha256 |

#### 失败处理与验收

- 失败与降级：Kafka 中断：本地 spool 后重试；不丢事件。；索引失败：PG 可查，标记索引延迟。
- 验收：哈希链可验证；删除被禁止；导出可追溯。

### 7.17 ca-evolution（ca-evolution）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S17 / 横切 |
| 代码仓 / 端口 | ca-evolution / 8017 |
| 副本 / 资源 | 2–4；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 评测任务异步；API P95 200ms |
| 生成顺序 | P5 |

自进化引擎：目标与评测管理、评测沙箱、候选改进、统计门禁、灰度发布与回滚。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不直接在线变更生产；不训练基础模型。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/goals | 创建迭代目标与评测集 |
| POST | /v1/candidates | 登记候选改进 |
| POST | /v1/evaluations | 运行评测（异步） |
| POST | /v1/releases | 灰度/全量发布 |
| POST | /v1/rollback | 回滚版本 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_evolution：goals、candidates、evaluations、releases；S3：eval-datasets/{goal}/、eval-artifacts/ |
| 依赖服务/中间件 | ca-agent-runtime；ca-sandbox；ca-observability；ca-model-gateway；Argo Rollouts；S3 |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 基线/候选同集评测；显著性检验后才允许发布。
- 评测集与生产集隔离并轮换，防过拟合；保留泄漏检测。
- 灰度发布按质量/成本/人工介入率门禁；异常自动回滚。
- 所有候选与发布版本不可变、可追溯。
| 类/模块 | 职责 |
| --- | --- |
| GoalService | 目标与评测集 |
| EvalHarness | 隔离评测执行 |
| CandidateRegistry | 候选与版本 |
| StatisticalGate | 显著性门禁 |
| ReleaseManager | 灰度/回滚 |

```text
ca-evolution/
  src/ca_evolution/
    main.py  config.py
    api/       # /v1/goals 等路由
    domain/    # GoalService, EvalHarness, CandidateRegistry
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| MIN_EVAL_SAMPLES | 200 |
| SIGNIFICANCE | 0.05 |
| CANARY_PERCENT | 5 |
| ROLLBACK_WINDOW_MIN | 30 |

#### 失败处理与验收

- 失败与降级：评测环境失败：不发布，保留候选。；灰度指标异常：自动回滚并冻结候选。
- 验收：发布需评测通过；回滚可验证；评测可复现。

### 7.18 ca-observability（ca-observability）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S18 / 横切 |
| 代码仓 / 端口 | ca-observability / 8018 |
| 副本 / 资源 | 3–12；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 写入 P95 200ms；查询 P95 1s |
| 生成顺序 | P5 |

可观测与任务追踪：OTLP 接入、Session/Trace/Span 查询、轨迹回放、成本归因与质量风险视图。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不产生业务事件；不做告警路由（交 Prometheus/Alertmanager）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/traces | OTLP/HTTP 接入 |
| GET | /v1/traces/{id} | Trace 与 Span 树 |
| GET | /v1/cost | 按任务/租户/模型成本 |
| GET | /v1/quality | 质量与人工介入指标 |
| POST | /v1/replay | 生成回放清单 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | ClickHouse：spans、cost_usage、quality_events；PostgreSQL ca_obs_meta：saved_views、replay_manifests；S3：trace-archive/ |
| 依赖服务/中间件 | OTel Collector；Kafka；ClickHouse；S3；Grafana |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- Trace 模型：session_id→trace_id→span_id，parent_span_id 建树。
- 采样：默认 10%，高风险任务 100%，错误全采。
- 成本按模型/工具/沙箱三源归因到 task/subtask。
- 回放清单引用检查点、Span、工具结果与验证证据。
| 类/模块 | 职责 |
| --- | --- |
| SpanIngestor | 批量写入 |
| TraceQuery | 树查询 |
| CostAttributor | 成本归因 |
| QualityAggregator | 质量指标 |
| ReplayService | 回放清单 |

```text
ca-observability/
  src/ca_observability/
    main.py  config.py
    api/       # /v1/traces 等路由
    domain/    # SpanIngestor, TraceQuery, CostAttributor
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| SAMPLE_RATE | 0.1 |
| BATCH_SPANS | 500 |
| RETENTION_DAYS | 90 |
| CH_DATABASE | ca_obs |

#### 失败处理与验收

- 失败与降级：ClickHouse 慢：降级采样并写 S3 缓冲。；查询超时：返回部分结果并标注。
- 验收：Trace 可完整还原主链路；成本可按任务拆解；回放可定位检查点。

### 7.19 ca-model-gateway（ca-model-gateway）

| 属性 | 规格 |
| --- | --- |
| 服务编号 / 平面 | S19 / 共享 |
| 代码仓 / 端口 | ca-model-gateway / 8019 |
| 副本 / 资源 | 4–30；2C/4Gi 请求，4C/8Gi 上限 |
| SLO | 首 Token P95 800ms |
| 生成顺序 | P0 |

模型统一网关：多供应商路由、回退、Token 预算、提示缓存、安全过滤、用量计量。

**负责：**本节 API、存储、依赖与关键设计所描述的能力。

**不负责：**不训练模型；不保存完整对话（仅摘要/用量）。

#### 接口设计

| 方法 | 路径 | 说明与关键字段 |
| --- | --- | --- |
| POST | /v1/chat | 对话补全（流式/非流式） |
| POST | /v1/embeddings | 向量化 |
| GET | /v1/models | 模型清单与能力 |
| POST | /v1/budgets | 预算设置/查询 |
| GET | /v1/usage | 用量与成本 |

#### 数据与依赖

| 类别 | 内容 |
| --- | --- |
| 存储 | PostgreSQL ca_model：providers、models、budgets；Redis：限额/缓存；Kafka：usage.events |
| 依赖服务/中间件 | 模型供应商；Vault/KMS；Redis；PostgreSQL；OTel；Kafka |
| 数据边界 | 仅拥有本节声明的库/表/桶；跨服务数据通过 API 或事件获取，禁止直连他库。 |

#### 关键技术设计与类职责

- 路由：按任务类型/成本/时延/上下文长度选择模型；失败回退。
- 预算：租户/任务/服务三级 Token 限额，超限拒绝或降级。
- 提示缓存：系统提示与稳定前缀缓存，降低重复成本。
- 安全：输入/输出过滤、敏感信息脱敏、供应商数据策略校验。
| 类/模块 | 职责 |
| --- | --- |
| ModelRouter | 选择与回退 |
| ProviderAdapter | 供应商协议 |
| BudgetManager | 限额与熔断 |
| PromptCache | 稳定前缀缓存 |
| SafetyFilter | 输入输出安全 |
| UsageMeter | 用量与成本 |

```text
ca-model-gateway/
  src/ca_model_gateway/
    main.py  config.py
    api/       # /v1/chat 等路由
    domain/    # ModelRouter, ProviderAdapter, BudgetManager
    infra/     # db, redis, kafka, s3, clients
    workers/   # outbox/consumer/timer
  migrations/ tests/ Dockerfile helm/ openapi.yaml
```

#### 配置与运行参数

| 配置项 | 说明/默认值 |
| --- | --- |
| DEFAULT_MODEL | planner-large |
| MAX_OUTPUT_TOKENS | 8192 |
| REQUEST_TIMEOUT_S | 60 |
| FALLBACK_CHAIN | provider-a,provider-b |

#### 失败处理与验收

- 失败与降级：主供应商限流：切换备用；全部失败返回 503 并保留预算。；超预算：拒绝并提示降级模型。
- 验收：路由可解释；预算准确；用量可归因到任务。

## 26 数据库与核心表设计

所有业务表使用 PostgreSQL 16，主键 UUIDv7，时间 timestamptz，软删除用 deleted_at；每库独立角色与连接池。以下为核心表的最小 DDL 基线，服务实现时以 Alembic 迁移为准。

### 26.1 任务与事件

```text
CREATE TABLE tasks (
  id uuid PRIMARY KEY, tenant_id text NOT NULL, project_id text,
  goal jsonb NOT NULL, constraints jsonb NOT NULL DEFAULT '{}',
  risk text NOT NULL DEFAULT 'medium', state text NOT NULL,
  version bigint NOT NULL DEFAULT 0, budget jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_tasks_tenant_state ON tasks(tenant_id, state, created_at DESC);
CREATE TABLE task_events (
  id bigserial PRIMARY KEY, task_id uuid NOT NULL REFERENCES tasks(id),
  event_type text NOT NULL, payload jsonb NOT NULL, trace_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_task_events_task ON task_events(task_id, id);
CREATE TABLE approvals (
  id uuid PRIMARY KEY, task_id uuid NOT NULL, reason text NOT NULL,
  status text NOT NULL DEFAULT 'pending', decided_by text,
  expires_at timestamptz, payload jsonb NOT NULL DEFAULT '{}'
);
```

### 26.2 DAG 与子任务

```text
CREATE TABLE dags (
  id uuid PRIMARY KEY, task_id uuid NOT NULL, plan_version int NOT NULL,
  state text NOT NULL, critical_path text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE dag_nodes (
  id uuid PRIMARY KEY, dag_id uuid NOT NULL REFERENCES dags(id),
  subtask_id uuid NOT NULL, node_type text NOT NULL, status text NOT NULL,
  priority int NOT NULL DEFAULT 100, global_lock boolean NOT NULL DEFAULT false
);
CREATE TABLE dag_edges (
  dag_id uuid NOT NULL, from_node uuid NOT NULL, to_node uuid NOT NULL,
  edge_type text NOT NULL DEFAULT 'data', PRIMARY KEY(dag_id, from_node, to_node)
);
CREATE TABLE subtasks (
  id uuid PRIMARY KEY, dag_id uuid NOT NULL, task_id uuid NOT NULL,
  kind text NOT NULL, spec jsonb NOT NULL, state text NOT NULL,
  attempts int NOT NULL DEFAULT 0, lease_until timestamptz,
  result_ref text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_subtasks_ready ON subtasks(state, lease_until);
```

### 26.3 检查点

```text
CREATE TABLE checkpoints (
  id uuid PRIMARY KEY, task_id uuid NOT NULL, subtask_id uuid,
  kind text NOT NULL, snapshot_ref text NOT NULL, checksum text NOT NULL,
  event_seq bigint NOT NULL, size_bytes bigint, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ckpt_task ON checkpoints(task_id, created_at DESC);
CREATE TABLE checkpoint_events (
  id bigserial PRIMARY KEY, checkpoint_id uuid NOT NULL, task_id uuid NOT NULL,
  seq bigint NOT NULL, event_type text NOT NULL, payload jsonb NOT NULL,
  idempotency_key text, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(task_id, seq)
);
CREATE TABLE watchdog_leases (
  task_id uuid PRIMARY KEY, last_heartbeat timestamptz NOT NULL,
  last_progress_at timestamptz NOT NULL, stalled boolean NOT NULL DEFAULT false
);
```

### 26.4 设计意图与计划

```text
CREATE TABLE design_intents (
  id uuid PRIMARY KEY, task_id uuid, source_refs jsonb NOT NULL,
  constraints jsonb NOT NULL, acceptance jsonb NOT NULL,
  component_map jsonb NOT NULL DEFAULT '{}', tokens jsonb NOT NULL DEFAULT '{}',
  risk text NOT NULL, confidence numeric(4,3) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE plans (
  id uuid PRIMARY KEY, task_id uuid NOT NULL, version int NOT NULL,
  schema_version text NOT NULL, plan jsonb NOT NULL,
  assumptions jsonb NOT NULL DEFAULT '[]', risks jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(task_id, version)
);
```

### 26.5 验证与记忆

```text
CREATE TABLE verifications (
  id uuid PRIMARY KEY, task_id uuid NOT NULL, subtask_id uuid,
  adapter text NOT NULL, status text NOT NULL, request jsonb NOT NULL,
  result jsonb, evidence_ref text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_verify_task ON verifications(task_id, created_at DESC);
CREATE TABLE memory_items (
  id uuid PRIMARY KEY, tenant_id text NOT NULL, scope text NOT NULL,
  task_id uuid, content_ref text NOT NULL, summary text NOT NULL,
  confidence numeric(4,3) NOT NULL, source jsonb NOT NULL,
  expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_memory_tenant_scope ON memory_items(tenant_id, scope, expires_at);
```

### 26.6 审计与自进化

```text
CREATE TABLE audit_events (
  id uuid PRIMARY KEY, tenant_id text NOT NULL, actor jsonb NOT NULL,
  action text NOT NULL, object jsonb NOT NULL, result text NOT NULL,
  trace_id text, prev_hash text, payload_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);
CREATE TABLE evolution_goals (
  id uuid PRIMARY KEY, name text NOT NULL, baseline jsonb NOT NULL,
  eval_set_ref text NOT NULL, threshold jsonb NOT NULL,
  status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
```

## 27 消息、缓存键与存储命名

### 27.1 Kafka Topic

| Topic | 生产者 | 消费者 | 保留/分区 |
| --- | --- | --- | --- |
| ca.task.events | ca-task | audit, observability | 7d / 24 |
| ca.subtask.events | ca-orchestration | sandbox, checkpoint, observability | 7d / 48 |
| ca.design.events | ca-design | planning, audit | 7d / 12 |
| ca.plan.events | ca-planning | orchestration, audit | 7d / 12 |
| ca.checkpoint.events | ca-checkpoint | sandbox, audit, observability | 7d / 24 |
| ca.tool.audit | ca-tool-gateway | audit, observability | 30d / 24 |
| ca.verify.events | ca-verification | task, planning, audit | 7d / 24 |
| ca.evolution.events | ca-evolution | observability, task | 30d / 12 |
| ca.trace.spans | OTel/服务 | observability | 3d / 48 |
| ca.usage.events | model-gateway | observability, audit | 30d / 24 |
| ca.human.approvals | ca-task | audit, observability | 30d / 12 |
| ca.dlq.<topic> | 各消费者 | 运维/重放 | 30d / 6 |

### 27.2 Redis 键空间

| Key 模式 | 用途 | TTL | 隔离 |
| --- | --- | --- | --- |
| ca:idem:{tenant}:{key} | 幂等响应快照 | 24h | 租户 |
| ca:lock:task:{id} | 任务迁移锁 | 30s 续约 | 任务 |
| ca:lease:{subtask} | 调度租约 | 60s | 子任务 |
| ca:rl:{tenant}:{route} | 令牌桶 | 滑动窗口 | 租户/路由 |
| ca:session:{id} | 会话指针/取消信号 | 2h | 会话 |
| ca:tool:idem:{key} | 工具调用幂等 | 24h | 租户/工具 |
| ca:cache:hot:{key} | 缓存热点元数据 | 1h | 租户 |
| ca:watchdog:{task} | 进展心跳 | 5m | 任务 |

### 27.3 对象存储与向量集合

| 资源 | 命名 | 内容 |
| --- | --- | --- |
| S3 Bucket | artifacts | 补丁、日志摘要、测试报告、截图 |
| S3 Bucket | checkpoints | 检查点 manifest 与增量块 |
| S3 Bucket | cache | 代码/依赖缓存内容块（内容寻址） |
| S3 Bucket | eval-datasets | 评测集、基线结果、候选产物 |
| Qdrant | memory_short | 任务短期记忆向量 |
| Qdrant | memory_long | 项目/经验长期记忆向量 |
| Qdrant | knowledge_docs | 私域知识分块向量 |

## 28 关键技术专题

### 28.1 两层调度

上层 DAG 调度负责依赖与全局推进，下层沙箱调度负责资源与并发。两者通过子任务租约解耦：上层只关心“就绪且未租出”，下层只关心“租约内可运行”。

```text
ready = {n for n in nodes if indegree[n]==0 and state[n]=='PENDING'}
while ready and capacity:
    n = max(ready, key=lambda x: 3*critical[x] + 2*priority[x] + age[x] - risk[x])
    lease = acquire_lease(n, ttl=60s)          # Redis SET NX PX
    if lease: dispatch_to_sandbox(n); ready.remove(n)
# 完成回调：幂等 + 减 indegree + 失败局部重规划
# 租约超时：state=PENDING, attempts+=1, 超限进入人工例外
```

沙箱端评分：cache_affinity（仓库/依赖命中）+ resource_fit（CPU/内存/GPU）− cold_start − fragmentation。评分最高且满足隔离约束的池获胜；无可用池则回压队列，不允许超卖。

### 28.2 缓存与动态挂载

```text
cache_key = sha256(repo_url + commit + deps_lock_hash + toolchain_version)
mount:
  1) 校验租户/仓库权限与签名
  2) 查 L1（节点本地块）→ L2（S3 内容块）
  3) 未命中回源拉取并写 S3；命中直接 COW 挂载
  4) 沙箱写时复制到可写层；卸载丢弃写层
invalidate: 主动(事件) + TTL + 回源校验；写路径禁止修改只读层
```

关键指标：命中率、回源率、挂载 P95、脏块比例。缓存污染通过内容寻址、签名、租户命名空间与写入审计控制。

### 28.3 共享记忆与混合检索

短期记忆面向任务上下文（TTL 72h），长期记忆面向项目约定与经验（需置信度与来源）。检索先做 ACL/租户过滤，再混合排序：

```text
score = 0.6 * cosine(query, item) + 0.3 * bm25(query, item) + 0.1 * recency(item)
top_k = 10; 结果必须带 source_ref、confidence、expires_at
写入策略：低置信不升长期；冲突写 memory_links；投毒可回滚
```

### 28.4 检查点、Stored Work 与恢复门

检查点将“元数据索引”与“增量 Blob”分离；恢复不是简单重启，而是“事实对齐”：

```text
create_checkpoint(task):
  snapshot = capture(worktree, plan, tool_state) -> S3
  insert checkpoints(checksum, event_seq, snapshot_ref)
recover(task):
  ckpt = latest_valid(); verify_checksum()
  gate = [anchor, repo/branch/base_commit, dirty_state,
          deps_lock, external_side_effects, permissions, ckpt_integrity]
  if all(gate): rebuild_worktree(); resume()
  else: rollback_previous() or escalate_human()
```

Stored Work：写操作先落 checkpoint_events（intent+idempotency_key），事务提交后才调用外部；恢复时先查询事实再决定重放/跳过。Watchdog 独立于主循环，双指标（心跳、实质进展）触发恢复或告警。

### 28.5 手脑分离与 Tool 调用

Runtime 无状态，Tool 有状态。每次调用链：Runtime 生成调用意图 → 执行器组装参数 → Tool 网关鉴权/策略/Schema/限流/幂等 → 受限执行 → 审计与结果回传。Runtime 不持有凭证，工具结果以事实写入检查点/证据，避免“脑”崩溃导致“手”状态丢失。

### 28.6 验证反馈与人工例外

验证结果标准化为 status/failure_class/root_cause/evidence/risk/confidence。决策矩阵：低风险高置信通过→自动推进；失败低风险→自动修复重试；高风险或低置信→人工例外。根因优先用规则（堆栈、变更关联、日志聚类），模型只做辅助并标注置信度。

### 28.7 自进化评测

```text
candidate vs baseline on same eval_set
  -> per-case delta; bootstrap CI; require p<0.05 and no critical regression
  -> canary 5% for 30min; watch quality/cost/human_rate
  -> pass: ramp; fail: rollback + freeze candidate
anti-overfit: eval_set rotation + holdout + production shadow
```

### 28.8 可观测与成本归因

Span 统一字段：trace_id、span_id、parent_span_id、task_id、subtask_id、sandbox_id、tool_id、model、tokens、cost、status。采样默认 10%，高风险 100%，错误全采。成本按模型/工具/沙箱三源归因，写 ClickHouse 并可按任务/租户聚合。

## 29 部署规格与容量

### 29.1 命名空间与节点池

| 命名空间 | 内容 | 隔离 |
| --- | --- | --- |
| ca-system | Envoy Gateway、identity、model-gateway、OPA、SPIRE | 系统级，最低网络开放 |
| ca-control | ingress/task/design/planning/orchestration/checkpoint | 控制面，禁止公网入口 |
| ca-exec | sandbox/runtime/executor/tool | 执行面，独立节点池 + gVisor |
| ca-data | cache/memory/knowledge/audit/observability | 数据面，限制外联 |
| ca-evolve | evolution | 隔离评测，独立配额 |
| ca-observability | OTel/Prometheus/Grafana/Loki | 只读采集，独立存储 |

| 节点池 | 规格 | 用途 | 扩缩 |
| --- | --- | --- | --- |
| system | 8C/32G × 3 | 网关/身份/策略 | 固定 + HPA |
| control | 8C/32G × 6 | 控制面服务 | HPA 3–12 |
| runtime | 16C/64G × N | Runtime/Executor | KEDA 按队列 10–200 |
| sandbox | 32C/128G × N（可 Spot） | 沙箱 | KEDA 按租约 |
| data | 16C/64G × 6 | 缓存/记忆/知识 | 垂直 + 分片 |

### 29.2 服务资源与扩缩

| 编号 | 服务 | 副本 | 资源（请求/上限） | SLO |
| --- | --- | --- | --- | --- |
| S01 | ca-ingress | 3–12 | 0.5C/512Mi 请求，2C/1Gi 上限 | P95 30ms；可用性 99.95% |
| S02 | ca-task | 2–8 | 1C/1Gi 请求，2C/2Gi 上限 | P95 100ms；状态迁移成功率 99.9% |
| S03 | ca-design | 2–6 | 0.5C/1Gi 请求，2C/2Gi 上限 | P95 3s；解析失败可重试 |
| S04 | ca-planning | 2–6 | 1C/2Gi 请求，2C/4Gi 上限 | P95 5s；计划校验 100% 通过 |
| S05 | ca-orchestration | 3–12 | 1C/2Gi 请求，2C/4Gi 上限 | P95 200ms；调度不丢任务 |
| S06 | ca-checkpoint | 3–9 | 1C/2Gi 请求，2C/4Gi 上限 | 写 P95 200ms；恢复 P95 60s |
| S07 | ca-sandbox | 2–6 | 1C/2Gi 请求，2C/4Gi 上限 | 调度 P95 50ms；热启动 30s |
| S08 | ca-agent-runtime | 10–200 | 2C/4Gi 请求，4C/8Gi 上限 | 单步 P95 8s（模型相关） |
| S09 | ca-executor | 5–100 | 1C/2Gi 请求，2C/4Gi 上限 | 执行开销 P95 1s（不含命令） |
| S10 | ca-tool-gateway | 3–20 | 1C/2Gi 请求，2C/4Gi 上限 | 网关开销 P95 20ms |
| S11 | ca-cache | 3–12 | 2C/4Gi 请求，4C/8Gi 上限 | 挂载 P95 500ms |
| S12 | ca-memory | 3–12 | 2C/4Gi 请求，4C/8Gi 上限 | 检索 P95 80ms |
| S13 | ca-knowledge | 2–8 | 2C/4Gi 请求，4C/8Gi 上限 | 检索 P95 120ms |
| S14 | ca-identity | 3–6 | 0.5C/512Mi 请求，1C/1Gi 上限 | P95 10ms |
| S15 | ca-verification | 2–8 | 1C/2Gi 请求，2C/4Gi 上限 | 提交 P95 100ms；结果回传 P95 5min |
| S16 | ca-audit | 3–9 | 1C/2Gi 请求，2C/4Gi 上限 | 写入 P95 50ms |
| S17 | ca-evolution | 2–4 | 2C/4Gi 请求，4C/8Gi 上限 | 评测任务异步；API P95 200ms |
| S18 | ca-observability | 3–12 | 2C/4Gi 请求，4C/8Gi 上限 | 写入 P95 200ms；查询 P95 1s |
| S19 | ca-model-gateway | 4–30 | 2C/4Gi 请求，4C/8Gi 上限 | 首 Token P95 800ms |

### 29.3 存储容量与容灾

| 组件 | 初始容量 | 扩容策略 | RPO/RTO |
| --- | --- | --- | --- |
| PostgreSQL | 2TB | 按库分表/分区；读副本 | RPO≤15min / RTO≤1h |
| ClickHouse | 10TB | 按月分区 + 冷归档 S3 | RPO≤1h / RTO≤4h |
| Qdrant | 2TB | 按 collection 分片/副本 | RPO≤1h / RTO≤2h |
| Kafka | 4TB/节点 | 增加分区/Broker | RPO≈0（RF3） |
| S3 | 按需 | 生命周期分层 | 版本化，跨区复制 |

### 29.4 网络与安全

- 南北向仅 Envoy Gateway 暴露；东西向 mTLS（服务网格/SPIFFE）。
- 沙箱默认拒绝出网，仅允许经 Tool 网关与缓存网关的受控出口。
- 数据库/缓存/消息仅数据面命名空间可达；控制面不直连对象存储（经服务）。
- 密钥由 Vault 注入；镜像签名 + SBOM + 只读根文件系统。

## 30 安全与合规实现

| 控制域 | 实现 | 验收 |
| --- | --- | --- |
| 身份 | SPIRE 工作负载身份 + 短期 JWT + 用户委托链 | 无长期静态凭证；委托链可追溯 |
| 授权 | RBAC+ABAC（租户/部门/仓库/环境/风险）+ OPA | 越权调用 100% 拒绝 |
| 密钥 | Vault/KMS 动态密钥；定期轮换 | 轮换无中断；泄露可吊销 |
| 工具 | Schema 校验、白名单、网络出口、双人复核 | 高危工具需审批 |
| 数据 | 分级、加密、脱敏、保留期、跨租户隔离 | 审计抽查通过 |
| 供应链 | 镜像签名、SBOM、依赖锁定、漏洞扫描 | CI 强制门禁 |
| 审计 | 哈希链、WORM、导出签名 | 不可篡改可验证 |

## 31 代码生成与实施计划

### 31.1 服务生成顺序与依赖门禁

| 阶段 | 服务 | 进入条件 | 完成标准 |
| --- | --- | --- | --- |
| P0 | ca-contracts、ca-identity、ca-model-gateway | 架构评审通过 | 契约仓发布 v0.1；身份/模型网关集成测试通过 |
| P1 | ca-ingress、ca-task、ca-design | P0 完成 | 任务创建/设计意图链路可跑通 |
| P2 | ca-planning、ca-orchestration、ca-checkpoint | P1 完成 | DAG 生成 + 检查点创建/恢复演示 |
| P3 | ca-sandbox、ca-cache、ca-tool-gateway、ca-agent-runtime、ca-executor | P2 完成 | 沙箱内脑手分离执行与缓存挂载跑通 |
| P4 | ca-memory、ca-knowledge、ca-verification、ca-audit | P3 完成 | 检索/验证回传/审计闭环 |
| P5 | ca-observability、ca-evolution、ca-web、ca-deploy | P4 完成 | Trace/成本可见，自进化可评测，GitOps 上线 |

### 31.2 单服务代码生成清单（DoD）

- 实现本节全部 API 与错误码；OpenAPI 与 ca-contracts 契约测试通过。
- 完成声明的表与迁移；关键索引存在；数据访问有超时与重试。
- 实现类职责表中的核心类；领域逻辑单测覆盖≥80%。
- 接入 OTel、结构化日志、/healthz、/metrics；SLO 指标可见。
- 完成失败与降级路径；幂等与并发冲突用例通过。
- Helm Chart 可部署到 dev；HPA/KEDA、PDB、NetworkPolicy 生效。
- 通过 CI：ruff、mypy、unit、integration、contract、image scan。

### 31.3 代码生成提示模板

对每个服务，将 7.x 小节与以下模板一起作为生成上下文，可获得结构一致的工程：

```text
请实现服务 <name>（仓库 <repo>，端口 <port>）。
约束：
- Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy async + Alembic
- 使用 ca-contracts 的模型与错误结构；不重复定义 DTO
- 目录结构遵循第 6.1 节；实现第 7.x 节全部 API 与类
- 存储仅使用本节声明的库/表/桶/键；外部调用经客户端并设置超时
- 接入 OTel 与结构化日志；提供 /healthz /readyz /metrics
- 为每个 API 写契约测试；领域逻辑单测覆盖≥80%
- 输出 Dockerfile、Helm Chart、openapi.yaml 与迁移脚本
```

## 32 附录

### 附录 A 服务 API 索引

| 服务 | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| ca-ingress | POST | /v1/tasks | 代理创建任务；校验 JWT、幂等键、租户配额 |
| ca-ingress | GET | /v1/tasks/{id} | 查询任务摘要；合并 task/quality 视图 |
| ca-ingress | POST | /v1/design-intents | 代理设计意图提交 |
| ca-ingress | GET | /v1/quality/{task_id} | 聚合质量看板摘要 |
| ca-ingress | POST | /v1/approvals/{id}:decide | 人工审批代理 |
| ca-ingress | GET | /healthz\|/readyz\|/metrics | 健康与指标 |
| ca-task | POST | /v1/tasks | 创建任务（幂等）；返回 task_id/state |
| ca-task | GET | /v1/tasks/{id} | 任务详情、当前 DAG、最近事件 |
| ca-task | POST | /v1/tasks/{id}:pause\|resume\|fork | 调用 checkpoint 并驱动状态迁移 |
| ca-task | POST | /v1/tasks/{id}/approvals | 创建/查询人工审批 |
| ca-task | POST | /v1/tasks/{id}/quality | 回写质量摘要（内部） |
| ca-task | GET | /v1/tasks | 按租户/状态/时间分页 |
| ca-design | POST | /v1/design-intents | 解析并保存设计意图 |
| ca-design | GET | /v1/design-intents/{id} | 查询意图与置信度 |
| ca-design | POST | /v1/design-sources/sync | 同步 Figma/工单变更 |
| ca-design | GET | /v1/design-sources/{id} | 版本与快照 |
| ca-design | GET | /v1/tokens/{version} | 获取 Token 快照 |
| ca-planning | POST | /v1/plans | 生成计划；入参 task_id/design_intent |
| ca-planning | GET | /v1/plans/{id} | 计划、假设、风险 |
| ca-planning | POST | /v1/plans/{id}/replan | 基于验证/设计变更重规划 |
| ca-planning | POST | /v1/decouple/analyze | 独立解耦分析 |
| ca-orchestration | POST | /v1/dags | 由计划构建 DAG |
| ca-orchestration | POST | /v1/dags/{id}/advance | 推进就绪节点（内部/定时） |
| ca-orchestration | GET | /v1/dags/{id} | DAG 状态与关键路径 |
| ca-orchestration | POST | /v1/subtasks/{id}/complete\|fail | 子任务结果回传 |
| ca-orchestration | POST | /v1/leases/acquire | 获取/续约子任务租约 |
| ca-checkpoint | POST | /v1/checkpoints | 创建检查点；返回 snapshot_ref |
| ca-checkpoint | GET | /v1/checkpoints/{id} | 查询元数据与校验和 |
| ca-checkpoint | POST | /v1/checkpoints/{id}:recover\|fork | 发起恢复/分叉 |
| ca-checkpoint | POST | /v1/watchdog/heartbeat | 任务进展心跳 |
| ca-checkpoint | GET | /v1/watchdog/{task_id} | 监督状态与告警 |
| ca-sandbox | POST | /v1/dispatch | 为子任务分配沙箱（含缓存挂载） |
| ca-sandbox | POST | /v1/sandboxes | 创建/复用沙箱；返回 sandbox_id |
| ca-sandbox | DELETE | /v1/sandboxes/{id} | 回收与清理 |
| ca-sandbox | POST | /v1/sandboxes/{id}/recover | 恢复门校验并重建工作树 |
| ca-sandbox | GET | /v1/pools | 池容量与命中率 |
| ca-agent-runtime | POST | /v1/sessions | 创建会话（task/subtask/context ref） |
| ca-agent-runtime | POST | /v1/sessions/{id}/step | 执行一步：推理+工具选择 |
| ca-agent-runtime | POST | /v1/sessions/{id}/cancel | 取消与预算终止 |
| ca-agent-runtime | GET | /v1/sessions/{id} | 当前步摘要（不保证持久化） |
| ca-executor | POST | /v1/executions | 执行子任务；返回 execution_id |
| ca-executor | POST | /v1/executions/{id}/patch | 生成/应用补丁 |
| ca-executor | POST | /v1/executions/{id}/test | 触发测试并收集证据 |
| ca-executor | GET | /v1/executions/{id}/evidence | 证据清单与引用 |
| ca-tool-gateway | POST | /v1/tools/{tool}/invoke | 调用工具；幂等键必填（写操作） |
| ca-tool-gateway | GET | /v1/tools | 工具清单与域 |
| ca-tool-gateway | POST | /v1/tools/register | 注册/升级工具版本（管理员） |
| ca-tool-gateway | GET | /v1/tools/{tool}/schema | 参数与返回 Schema |
| ca-tool-gateway | POST | /v1/policies/evaluate | 内部策略评估 |
| ca-cache | POST | /v1/mounts | 按 repo/commit/deps 挂载；返回 mount_id |
| ca-cache | DELETE | /v1/mounts/{id} | 卸载与清理 |
| ca-cache | POST | /v1/invalidate | 按 key/仓库失效 |
| ca-cache | GET | /v1/cache/entries/{key} | 查询元数据与命中 |
| ca-cache | POST | /v1/cache/warm | 预热常用仓库 |
| ca-memory | POST | /v1/memories | 写入记忆（来源/置信度/TTL） |
| ca-memory | POST | /v1/memories/retrieve | 混合检索（向量+关键词+时效） |
| ca-memory | DELETE | /v1/memories/{id} | 删除/失效 |
| ca-memory | POST | /v1/memories/consolidate | 巩固与去重 |
| ca-knowledge | POST | /v1/documents | 上传/登记文档与代码路径 |
| ca-knowledge | POST | /v1/documents/{id}/reindex | 重建索引 |
| ca-knowledge | POST | /v1/search | 检索（带 ACL 与引用） |
| ca-knowledge | DELETE | /v1/documents/{id} | 删除与索引清理 |
| ca-knowledge | GET | /v1/documents/{id} | 版本/状态 |
| ca-identity | POST | /v1/token | 签发短期访问令牌 |
| ca-identity | POST | /v1/introspect | 校验令牌与返回声明 |
| ca-identity | POST | /v1/authorize | 资源/动作授权决策 |
| ca-identity | GET | /.well-known/jwks.json | 公钥 |
| ca-identity | POST | /v1/revoke | 吊销令牌/主体 |
| ca-verification | POST | /v1/verifications | 提交验证请求 |
| ca-verification | POST | /v1/callbacks/{adapter} | 接收结果回调（签名校验） |
| ca-verification | GET | /v1/verifications/{id} | 结果与证据 |
| ca-verification | POST | /v1/verifications/{id}/retry | 重试验证 |
| ca-audit | POST | /v1/events | 写入审计事件（内部） |
| ca-audit | GET | /v1/events | 按主体/对象/时间检索 |
| ca-audit | GET | /v1/events/{id} | 事件详情 |
| ca-audit | POST | /v1/export | 合规导出（签名） |
| ca-evolution | POST | /v1/goals | 创建迭代目标与评测集 |
| ca-evolution | POST | /v1/candidates | 登记候选改进 |
| ca-evolution | POST | /v1/evaluations | 运行评测（异步） |
| ca-evolution | POST | /v1/releases | 灰度/全量发布 |
| ca-evolution | POST | /v1/rollback | 回滚版本 |
| ca-observability | POST | /v1/traces | OTLP/HTTP 接入 |
| ca-observability | GET | /v1/traces/{id} | Trace 与 Span 树 |
| ca-observability | GET | /v1/cost | 按任务/租户/模型成本 |
| ca-observability | GET | /v1/quality | 质量与人工介入指标 |
| ca-observability | POST | /v1/replay | 生成回放清单 |
| ca-model-gateway | POST | /v1/chat | 对话补全（流式/非流式） |
| ca-model-gateway | POST | /v1/embeddings | 向量化 |
| ca-model-gateway | GET | /v1/models | 模型清单与能力 |
| ca-model-gateway | POST | /v1/budgets | 预算设置/查询 |
| ca-model-gateway | GET | /v1/usage | 用量与成本 |

### 附录 B 代码仓结构基线

```text
ca-contracts/
  proto/ openapi/ events/ python/ca_contracts/
ca-deploy/
  helm/ terraform/ argocd/ policies/ environments/
ca-web/
  app/ components/ lib/ tests/
```

### 附录 C 环境变量基线

| 变量 | 适用服务 | 说明 |
| --- | --- | --- |
| DATABASE_URL | 有库服务 | PostgreSQL DSN（Vault 注入） |
| REDIS_URL | 使用 Redis 的服务 | Cluster DSN |
| KAFKA_BOOTSTRAP | 事件服务 | Broker 列表 |
| OTEL_EXPORTER_OTLP_ENDPOINT | 全部 | Collector 地址 |
| IDENTITY_URL | 除 identity 外 | 令牌/授权服务 |
| MODEL_GATEWAY_URL | runtime/planning/design/memory/knowledge/verify | 模型网关 |
| S3_ENDPOINT/BUCKET | checkpoint/executor/cache/evolution | 对象存储 |
| LOG_LEVEL | 全部 | INFO/DEBUG |

### 附录 D 验收与追溯

每个服务的验收记录需包含：契约测试报告、单测覆盖率、集成测试、压测 P95/P99、SLO 仪表盘链接、审计事件样例、故障演练结论。验收通过后，方可在 ca-deploy 中开启上游流量。

— 文档结束 —
