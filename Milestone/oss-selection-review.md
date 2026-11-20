# 架构图逐模块选型复核与开源复用分析

日期：2026-11-19。对象：[架构图](index.html) 的 64 个模块（原图 50 个 + V2 新增 14 个），现状以提交 `207afd8` 为准。
方法：对每个模块看三件事：现在的选型合不合理；有没有成熟开源项目可以直接复用或替换；结论是什么。
开源项目的现状参考了 2026 年的公开资料（见文末来源）。很多对比文章是厂商自己写的，许可证和维护状态以项目仓库为准，
标“需核实”的条目在采用前要再确认。

评价标记：

| 标记 | 含义 |
|---|---|
| ✅ 保持 | 选型合理，开源方案没有明显优势 |
| ➕ 补强 | 现有选型保留，但应接入某个开源项目补一块能力 |
| 🔁 替换 | 自研部分应换成开源项目，或在规模化前换掉 |
| 🆕 引入 | 尚未实现，建议直接用开源项目，不要自研 |

---

## 0 总结论

**整体选型是合理的，底座基本都已经是业界主流开源件**：Temporal、Postgres（RLS + pgvector）、LiteLLM、
OpenTelemetry/Prometheus/Jaeger/Grafana、OpenBao、Squid、Calico、gVisor、SeaweedFS、Kubernetes。
真正自研的部分，大多是开源生态里**没有现成答案**的部分：评测闭环、轨迹检查、脑手分离、按轮签发密钥、版本构建谱系。
这部分是平台的差异化，应该继续自研。

需要调整的主要是以下 8 点，按优先级排列：

1. **工具域：直接用 MCP + 开源 MCP 网关，不要自研**（agentgateway / IBM ContextForge / Docker MCP Gateway 三选一）。这是架构图上唯一完全没做的模块，也是开源最成熟的一块。
2. **记忆：保留自研的“治理层”，把“抽取和检索引擎”换成可插拔的开源引擎**（首选 Graphiti，其次 Mem0）。不要用 Letta。见第 1 节专题。
3. **网关限流代理：不要继续加码自研**。多副本共享限流用 LiteLLM 自带的 Redis 限流；规模再大换 Envoy AI Gateway 或 agentgateway。
4. **K8s 沙箱预热池：评估 `kubernetes-sigs/agent-sandbox` 替代自研预热池；集群级配额叠加 Kueue**。自研准入管“任务能否开始”（Temporal 层），Kueue 管“Pod 能否调度”（集群层），两者是叠加关系，自研准入保留。
5. **沙箱隔离：在 SandboxProvider 下加 Kata 或 gVisor 的 RuntimeClass**，需要 microVM 时再评估 E2B 自托管版或 microsandbox。空闲冻结改用 CRIU/microVM 快照，不再用 `docker pause`。
6. **工作负载身份和细粒度授权：引入 SPIFFE/SPIRE + OpenFGA**，替代“只有用户/租户角色”的现状。
7. **LLM 专用观测：在 OpenTelemetry 之上接 Langfuse**（MIT），拿到 prompt/会话级的追踪和评测数据集，不要自己做 LLM 观测 UI。
8. **企业知识库：文档用 Onyx 一类的现成产品，代码用 Zoekt + SCIP/tree-sitter 索引**，不要自研连接器和权限同步。

**不建议替换的**：Temporal（DAG/队列/检查点/再优化）、Postgres + RLS、LiteLLM 作为模型网关、
Portal 评测闭环、OpenCode 构建与版本谱系、脑手分离、轨迹检查。这几块要么已经是最佳开源件，要么开源里没有对应物。

**许可证红线**（平台核心件避免）：Elastic License 2.0（如 Arize Phoenix）、AGPL（如 MinIO、部分沙箱项目）、
“开源核心 + 关键能力在企业版”的项目（采用前逐项核对）。Daytona 2026 年有转闭源的报道（需核实），不建议押注。

---

## 1 专题：大规模多 Agent 场景下的记忆模块

### 1.1 我们的需求

在“很多 Agent 并发跑、跨租户、跨仓库”的场景下，记忆系统要同时满足：

| 需求 | 说明 |
|---|---|
| 作用域 | 租户 → 团队 → 仓库 → Agent → 单次运行，读取时只能看到自己作用域或更宽的记忆 |
| 治理 | 写入要审核；每条记忆有来源（哪个任务、哪个轮次）；能纠正、能过期、能撤回 |
| 时效 | 代码约定会变（“用 pytest” → “改用 vitest”），旧事实要失效，而不是和新事实并存 |
| 并发写入 | 上百个 Agent 同时提议记忆，要去重和合并 |
| 隔离 | 多租户不能串，最好在数据库层强制（RLS） |
| 检索质量 | 语义检索 + 关键字 + 按仓库路径过滤 |

现状：Postgres + RLS + pgvector，有作用域、审核、来源和审计，检索是向量 + 过滤。
**治理和隔离已经做得比大多数开源项目好；弱项是抽取质量、时效处理和图结构关系。**

### 1.2 开源候选

| 项目 | 许可证 | 定位 | 多 Agent / 多租户 | 时效 | 评价 |
|---|---|---|---|---|---|
| **Graphiti**（Zep 的开源引擎） | Apache-2.0 | 时序知识图谱：实体为节点、事实为带有效期的边 | 有 group_id 分区，没有审核流程 | **强**：事实有生效/失效时间，能回答“某约定何时变的” | 最适合“代码约定会变”的场景。需要图数据库（Neo4j / FalkorDB 等） |
| **Mem0** | Apache-2.0 | 记忆抽取 + 向量（可选图）存储 | user / agent / run 多标识作用域 | 中：有更新/删除冲突合并 | 社区最大、接入最快；最顺的用法是它的托管平台，自托管要自己补治理 |
| **Cognee** | Apache-2.0 | 图优先的知识/记忆引擎，带 MCP 服务 | 数据集级隔离 | 中 | 偏“文档 → 知识图谱”，更适合知识库那一块 |
| **Letta**（原 MemGPT） | Apache-2.0 | **完整的 Agent 运行时**，记忆是其中一部分 | 共享记忆块 | 中 | **不适合**：要求 Agent 跑在它的运行时里，和我们的可插拔后端（Codex/OpenCode/pi）冲突 |
| Zep 社区版 | — | — | — | — | 2025 年已停止，只能自托管 Graphiti |
| Caura（原 MemClaw）、KD6、腾讯 Team Memory | 各异 | 多租户/层级作用域的共享记忆服务 | 强 | 弱–中 | 方向对，但项目年轻、社区小；腾讯方案被指出缺纠错和过期机制。可参考设计，不建议依赖 |

### 1.3 结论

**保留自研记忆服务作为“治理层”，把抽取和检索做成可替换的引擎接口；首选接 Graphiti。**

- 开源项目普遍缺我们最在意的部分：写入审核、来源追溯、数据库层多租户强制、与任务事件打通的审计。
  这些我们已经做了，换掉反而倒退。
- 我们缺的是：从轨迹里**抽取**高质量记忆、**时效**（旧约定失效）、实体关系。这正是 Graphiti 的强项。
- 做法：定义 `MemoryEngine` 接口（`extract / upsert / search / invalidate`）。现有 pgvector 实现作为默认，
  Graphiti 作为第二实现。审核通过的记忆才写入引擎；检索结果仍经过 RLS 作用域过滤后再给 Agent。
- Mem0 作为备选：如果只需要“更好的抽取 + 去重”，不需要时序图，Mem0 接入成本更低。
- 规模上限：pgvector 在千万级向量以内足够；超过再考虑 Qdrant/Milvus（见向量存储一行）。
- **前置条件：真实 embeddings 模型。** 目前 DeepSeek 没有 embeddings 接口，任何记忆引擎的检索质量都还没法真实验证，这是比换引擎更先要解决的事。

---

## 2 逐模块分析

### 2.1 人类工作入口层

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 目标与任务指定 `ui_goal` | FastAPI + Pydantic + Typer CLI | ✅ 保持 | — | API 优先的做法正确，Python 生态主流 |
| 任务下发 `ui_dispatch` | FastAPI + Postgres + Temporal | ✅ 保持 | — | 持久排队用 Temporal 是对的 |
| 任务质量看板 `ui_quality` | 单文件 HTML/JS + Grafana | ➕ 补强 | Langfuse（数据集/实验视图）、Grafana | Portal 适合演示；产品化时界面改用 React 等正经前端框架。通用指标继续放 Grafana，LLM 质量数据交给 Langfuse，只自研“评测矩阵/版本对比”这部分 |
| 人工确认 / 再优化 `ui_confirm` | Temporal Signal | ✅ 保持 | — | 人工介入就该用 Signal |
| 人工例外审批 `ui_approve` | Temporal + 角色授权 | ➕ 补强 | OpenFGA（按资源/风险的授权关系） | 审批流本身保留 Temporal；“谁能批什么”交给 OpenFGA，就不用写死在代码里 |
| 任务历史与回放 `ui_history` | SSE + Postgres LISTEN/NOTIFY | ✅ 保持 | Langfuse Sessions（可选） | 编码场景的泳道和证据面板是差异化功能，开源里没有对应物 |

### 2.2 左翼 · 设计系统对接区

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 设计稿与工单接入 `design_in` | 文件上传 + IntentAdapter | 🆕 引入 | Figma REST API / Figma MCP Server；工单用 Jira/GitHub Issues/Linear 官方 MCP | 不要自研连接器，统一走 MCP（和工具域共用一套网关） |
| 设计规范与 Token `design_spec` | Ruff、Playwright 规则 | 🆕 引入 | Style Dictionary / Tokens Studio 格式（W3C Design Tokens） | 采用 W3C Design Tokens 格式，用 Style Dictionary 解析，不要自定格式 |
| 设计意图解析器 `design_intent` | 自研 IntentBundle + Schemathesis + Playwright | ✅ 保持 | — | 把意图编译成可执行检查是差异化；用到的检查工具已是开源主流 |

### 2.3 右翼 · 验证反馈系统对接区

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 验证反馈网关 `verify_gw` | 命令验证 + GitHub/Gitea Actions 回调 | ✅ 保持 | — | 复用现有 CI 是对的 |
| 结构化验证解析器 `verify_parser` | 退出码 + 自定 CheckResult | ➕ 补强 | **SARIF**（静态分析标准格式）、**JUnit XML**、CTRF；Semgrep / CodeQL 的 SARIF 输出 | 输入侧统一解析 SARIF 和 JUnit XML，就能接住绝大多数扫描器和测试框架，不必逐个写解析器 |
| 质量回显与例外审批 `quality_echo` | 事件日志 + Signal | ➕ 补强 | GitHub Checks API / PR 评论 | 回写外部平台优先走 Checks API |

### 2.4 接入与编排段

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 任务接入与统一鉴权 `task_entry` | FastAPI + OIDC(Dex) + Postgres RLS | ✅ 保持 | Keycloak（如需完整 IdP） | Dex 足够做 OIDC 联邦；要用户管理界面时换 Keycloak。RLS 做租户隔离是强项 |
| 任务理解与规划器 `planner` | Codex 规划 + Pydantic + pathspec | ✅ 保持 | — | 用 Agent 本身做规划是主流做法 |
| 软件工程解耦分析器 `decouple` | pathspec 路径范围冲突检测 | ➕ 补强 | **tree-sitter**、**SCIP / LSIF 代码索引**（scip-python、scip-typescript 等）、stack-graphs | 语义依赖和影响面分析不要自研解析器，用 SCIP 索引出“谁引用谁” |
| 全局 DAG 编排器 `dag` | Temporal 替代自研 DAG | ✅ 保持 | （对比：Argo Workflows、Airflow、Dagster、Prefect） | Temporal 适合长时间、带人工信号、需要可靠重放的 Agent 任务；Argo 更偏批处理，Airflow/Dagster 偏数据管道。**不换** |
| 检查点与续存管理 `checkpoint` | Git bundle + S3 + SHA-256 | ✅ 保持 | CRIU（进程级检查点，可选） | 代码状态用 Git 是正解；进程级检查点留给沙箱快照（见 2.5） |
| 子任务队列与调度 `subtask_q` | Temporal Child Workflow + git merge-tree | ✅ 保持 | — | 不需要独立队列服务 |
| 再优化任务入口 `optim_entry` | Temporal Signal + 恢复会话 | ✅ 保持 | — | — |
| 自进化触发器 `evo_trigger` | Portal 手动触发 | ➕ 补强 | Temporal Schedules；告警驱动可用 Alertmanager webhook | 自动触发直接用 Temporal Schedule + 评测回归告警，不要另建调度器 |

### 2.5 Agent 运行时与沙箱执行段

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 沙箱调度器 `sandbox_sched` | 自研准入队列 + Provider + K8s 放置 | ➕ 补强 | **Kueue**（K8s 原生作业排队、配额、公平共享）、**kubernetes-sigs/agent-sandbox**（需核实成熟度） | 自研任务级准入保留；K8s 生产环境叠加 Kueue 做集群级配额。SandboxProvider 抽象保留 |
| 沙箱池与并发实例 `sandbox_pool` | 自研 K8s 固定大小预热池 | 🔁 替换（K8s） | **kubernetes-sigs/agent-sandbox**（Sandbox CRD、预热池、gVisor/Kata，2025 年底发布，需核实成熟度）；E2B 自托管版（Firecracker，自托管工作量大） | 优先评估 agent-sandbox 做 K8s Provider 的底层，去掉自研预热池；保留我们自己的工作区传输和出口控制 |
| Agent Runtime `runtime` | 可插拔后端：Codex / OpenCode / brain / pi | ✅ 保持 | OpenHands（可作为又一个后端）、Aider | 可插拔接口是正确选择，不锁定单一 Agent。可以考虑加 OpenHands 后端，扩大评测对照面 |
| 子任务执行与装配 `executor` | Temporal Activity + TaskKind | ✅ 保持 | — | — |
| 会话恢复门 `recover_gate` | Git 提交 + manifest + 会话恢复 | ✅ 保持 | — | 开源里没有对应物 |
| Tool 网关与工具域 `hand_brain` | 脑手分离（OpenCode WorkspaceDriver）+ Docker/K8s | ✅ 保持 / ➕ | MCP（工具协议）、E2B envd（执行守护进程，可参考） | 脑手分离的方向和业界一致（OpenHands、E2B 都是“控制面在外、执行面在沙箱”）。工具协议向 MCP 靠拢 |

### 2.6 共享服务段

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 共享记忆服务 `memory` | Postgres RLS + pgvector + 审核 | ➕ 补强 | **Graphiti**（首选）、Mem0（备选）；不用 Letta | 见第 1 节：保留治理层，引擎可插拔 |
| Agent 身份与权限 `identity` | OIDC + JWT + RLS + LiteLLM 临时密钥 | ➕ 补强 | **SPIFFE/SPIRE**（工作负载身份）、**OpenFGA**（关系型细粒度授权，CNCF） | 按轮签发模型密钥是亮点，保留。“哪个沙箱是谁”用 SPIFFE 证书身份；“谁能用哪个工具/仓库”用 OpenFGA |
| 企业私域知识库 `knowledge` | Postgres + pgvector 短记忆 | 🆕 引入 | **Onyx**（MIT，40+ 连接器、继承源系统权限；部分能力在企业版，需核对）、PipesHub、RAGFlow（Apache-2.0，偏文档解析）；代码检索用 **Zoekt**（Apache-2.0） | 文档连接器和权限同步是重活，不要自研。通过 MCP 把 Onyx 和 Zoekt 当工具暴露给 Agent |
| 部门工具域 `tool_domain` | 未实现 | 🆕 引入 | **MCP** + MCP 网关：**agentgateway**（Linux 基金会旗下）、**IBM ContextForge**（Apache-2.0，带注册中心）、**Docker MCP Gateway**（每个工具一个隔离容器）；授权配 OpenFGA | 最该直接复用的一块。推荐 agentgateway 或 ContextForge 做注册、路由、鉴权、审计；需要强隔离运行第三方工具时用 Docker MCP Gateway |
| 审计与合规服务 `audit` | Postgres 事件 + 对象归档 | ➕ 补强 | S3 Object Lock / WORM 桶（SeaweedFS、Ceph 支持）、OpenTelemetry Logs | 防篡改导出不用自研，归档写到开了对象锁的桶即可 |

### 2.7 缓存服务区

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 代码缓存服务 `cache_code` | Git bare mirror + 文件锁 + 只读基础层 | ✅ 保持 | Gitaly（GitLab 的 Git 存储服务，跨节点时参考）、git 协议 v2 的 bundle-uri | 单机足够；跨节点时用 bundle-uri + 对象存储分发 |
| 依赖与二进制缓存 `cache_dep` | pip 缓存 + OpenCode 只读依赖挂载 | 🔁 规模化时替换 | **Nexus Repository OSS / Pulp**（多语言包代理）、devpi（pip）、Verdaccio（npm）、Athens（Go）；编译缓存 **sccache**、**bazel-remote**；镜像层用 BuildKit cache | 跨节点时不要自研分发，统一用包代理（顺便让出口白名单只放行代理）。单机保持现状 |
| 缓存挂载网关 `cache_gw` | mount / sync / overlay 传输 | ✅ 保持 | JuiceFS（共享 POSIX 文件系统，跨节点时可选） | 传输抽象是自研的价值所在；跨节点共享存储可评估 JuiceFS |

### 2.8 存储与算力底座

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 对象存储 `object_store` | ObjectStore 接口 + SeaweedFS / S3 | ✅ 保持 | Ceph RGW（大规模）、Garage（轻量） | SeaweedFS 选得对；MinIO 社区版 2025 年起功能收缩、许可证为 AGPL，不建议改用 |
| 向量索引存储 `vector_store` | pgvector | ✅ 保持 | pgvectorscale（大规模扩展）；Qdrant / Milvus（千万级以上） | 千万级向量以内 pgvector 最省事（同库、同 RLS）；规模上去再拆 |
| 代码仓与制品库 `repos` | Git + Gitea / GitHub + S3 | ➕ 补强 | **Harbor** 或 **zot**（OCI 镜像/制品仓库） | 沙箱镜像和 OpenCode 构建产物可以作为 OCI 制品存进 Harbor/zot，版本谱系就有统一存储 |
| 容器与 K8s 集群 `k8s` | Docker + K8s + kind + Calico | ✅ 保持 | Cilium（eBPF，FQDN 级出口策略）可评估 | Calico 够用；如果想在网络层按域名放行出口，Cilium 的 FQDN 策略比 Squid 更原生 |
| CPU / GPU 算力池 `compute` | 资源限额 + CPU HPA 样例 | ➕ 补强 | **KEDA**（按队列长度伸缩 Worker）、Karpenter / Cluster Autoscaler、NVIDIA GPU Operator、Kueue | Worker 按 Temporal 队列积压伸缩，用 KEDA 的 Temporal 伸缩器（需核实版本）或自定义指标 |
| 密钥与证书 KMS `kms` | OpenBao + GitHub App JWT | ✅ 保持 / ➕ | **cert-manager**（证书签发与轮换）、External Secrets Operator | OpenBao（Vault 的开源分支，MPL）选得对；出口 CA 的轮换交给 cert-manager |
| 日志与追踪存储 `obs_store` | Postgres 事件 + OTel + Prometheus + Jaeger + Grafana | ✅ 保持 / ➕ | Grafana Tempo / Loki、ClickHouse（大规模事件分析） | 现在合理；量大后追踪换 Tempo、日志用 Loki，事件分析用 ClickHouse |
| 网络与安全基础设施 `net_sec` | Squid TLS 检查 + Calico + gVisor 可选 | ✅ 保持 / ➕ | Envoy（替代 Squid 做 TLS 出口代理）、Cilium FQDN 策略、**Kata Containers** | Squid 能用且已验证。隔离强度上，多租户生产环境应默认 gVisor 或 Kata，而不是普通容器 |

### 2.9 自进化引擎

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 目标与评测管理 `evo_goal` | YAML 套件 + `sca eval` | ✅ 保持 / ➕ | **Inspect AI**（UK AISI，MIT）、SWE-bench 测评框架、Terminal-Bench | 编码 Agent 的评测（隐藏验收、真实沙箱）开源里没有可整体替换的。建议套件格式兼容 SWE-bench 实例格式，方便导入公开基准做对照 |
| 评测沙箱与结果 `evo_eval` | Temporal 评测工作流 + 隔离沙箱 + 轨迹检查 | ✅ 保持 | Langfuse / Opik（评测结果与数据集管理） | 执行层保留；结果和数据集可以同步到 Langfuse，方便对比 |
| 策略与代码改进 `evo_improve` | 人工诊断后由 Agent 修源码 | ✅ 保持 | DSPy / GEPA（提示词自动优化，可选） | 改代码这条路是差异化；改 prompt 那条路可以用 DSPy 一类工具 |
| 版本发布与回滚 `evo_release` | OpenCode 构建注册 + 显式选版 | ➕ 补强 | **OpenFeature + flagd** 或 Unleash（按租户/比例灰度）、Argo Rollouts（部署级灰度） | 灰度和回滚用特性开关实现：版本号作为开关值，按租户放量，不要自研 |

### 2.10 可观测与任务追踪

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| Session→Trace→Span `obs_trace` | OpenTelemetry + Temporal tracing + Jaeger | ✅ 保持 | OpenLLMetry / OTel GenAI 语义约定 | 加上 GenAI 语义约定的属性，就能直接被 Langfuse 等工具读取 |
| 轨迹回放与可视化 `obs_replay` | Portal 泳道 + 事件归档 | ✅ 保持 / ➕ | Langfuse（LLM 调用级追踪 UI） | 编码轨迹的泳道和证据规则保留自研；纯 LLM 调用细节交给 Langfuse |
| Token 与成本归因 `obs_cost` | LiteLLM spend + Postgres + Prometheus | ✅ 保持 | Langfuse 成本视图 | LiteLLM 自带的计费已经足够 |
| 质量视图与风险回溯 `obs_risk` | Grafana + 规则诊断 + eval compare | ➕ 补强 | Langfuse / Opik（评分与标注）、Grafana | 人工介入率等指标放 Grafana；人工标注用 Langfuse 的标注队列 |

### 2.11 V2 新增能力

| 模块 | 现在的选型 | 评价 | 可复用的开源 | 结论 |
|---|---|---|---|---|
| 数据分析场景 `analysis_kind` | TaskKind + Python | ✅ 保持 | Jupyter kernel（执行环境，可选） | 场景插件机制正确 |
| 按轮签发的模型网关 `model_gateway` | LiteLLM + 按轮密钥 | ✅ 保持 | （对比：Envoy AI Gateway、agentgateway、Bifrost、Portkey 网关） | LiteLLM 仍是最常被引用的基线，按轮密钥用的就是它的密钥 API。性能成瓶颈时再评估 Go 写的 Bifrost 或 Envoy AI Gateway |
| Portal 评测→修复→回归 `portal_loop` | 自研 | ✅ 保持 | — | 核心差异化，开源里没有对应物 |
| OpenCode 构建与版本注册 `opencode_versions` | Bun + 断网构建 + 元数据 | ✅ 保持 / ➕ | Harbor/zot 存产物；SLSA / in-toto（构建来源证明） | 产物推到 OCI 仓库，附 SLSA 来源证明，谱系就更标准 |
| OpenCode 脑手分离 `brain_split` | OpenCode EmbeddedHost + docker exec | ✅ 保持 | — | 与上游 OpenCode 的设计一致 |
| 常驻 brain 池 `brain_pool` | Unix socket + NDJSON + SQLite | ✅ 保持 | — | — |
| 评测轨迹检查与重评分 `trajectory_checks` | 自研规则 | ✅ 保持 | LLM-as-judge 评估器（Opik、DeepEval，可选） | 规则是确定性的，保留；可以再加 LLM 评审作为第二信号 |
| 准入控制与加权排序 `admission_control` | Postgres advisory lock + Temporal timer | ✅ 保持 | Kueue（集群层，叠加） | 任务级准入（租户公平、优先级、网关反压）开源里没有对应物，保留；K8s 生产再叠加 Kueue |
| 模型网关限流代理 `gateway_limiter` | 自研 ASGI 代理 | 🔁 替换 | **LiteLLM 自带的 RPM/TPM 限流 + Redis**（多副本共享）；Envoy AI Gateway / agentgateway（按 token 限流） | 不要再投入自研。先换成 LiteLLM 自带限流（多副本经 Redis 共享），保留我们的“反压文件”只作为准入信号 |
| 只读层与 overlay 工作区 `workspace_overlay` | Docker volume + overlayfs | ✅ 保持 | — | overlayfs 是标准做法 |
| 准备快照与分叉 `workspace_forks` | 内容寻址 + tar + overlay/reflink | ✅ 保持 / ➕ | ZFS/btrfs 快照、Firecracker 快照 | 文件级快照保留；做 Best-of-N 时，如果要连进程状态一起分叉，再考虑 microVM 快照 |
| pi 编码后端 `pi_backend` | pi 1.5.2 | ✅ 保持 | — | — |
| 沙箱空闲冻结 `idle_suspend` | Docker pause | 🔁 替换方案 | **CRIU**（容器检查点/恢复，可释放内存）、Firecracker/Cloud Hypervisor 快照、Kata | 实测 `docker pause` 不省内存，方向不对。真要省内存，用 CRIU 把进程写盘或 microVM 快照；短期保持关闭 |
| 沙箱长连接执行 handd `handd` | 自研 NDJSON 守护进程 | ✅ 保持 | E2B envd（同类设计，可参考） | 体量小、收益已验证，自研合适 |

---

## 3 按决策汇总

| 决策 | 模块 |
|---|---|
| 🆕 直接引入开源（未实现部分） | 部门工具域（MCP + agentgateway/ContextForge）；企业知识库（Onyx + Zoekt）；设计稿接入（Figma/工单 MCP）；设计 Token（Style Dictionary） |
| 🔁 替换自研 | 网关限流代理（→ LiteLLM 自带限流 + Redis）；K8s 预热池（→ agent-sandbox）；空闲冻结（→ CRIU/microVM 快照）；规模化时的依赖缓存（→ 包代理） |
| ➕ 接入开源补强 | 记忆（Graphiti/Mem0 引擎）；身份（SPIFFE/SPIRE + OpenFGA）；LLM 观测（Langfuse）；解耦分析（SCIP/tree-sitter）；验证解析（SARIF/JUnit）；发布灰度（OpenFeature/Unleash）；伸缩（KEDA）；CA 轮换（cert-manager）；制品（Harbor/zot）；隔离（gVisor/Kata 默认） |
| ✅ 保持 | 其余模块，包括 Temporal 编排、Postgres/RLS、LiteLLM 网关、OTel 栈、OpenBao、SeaweedFS、评测闭环、脑手分离、轨迹检查、版本谱系、handd |

## 4 建议的落地顺序

1. **工具域接 MCP 网关**（agentgateway 或 ContextForge），顺带把知识库（Onyx、Zoekt）和设计/工单接入都以 MCP 工具的形式挂上去，一次解决三个模块。
2. **限流代理换成 LiteLLM 自带限流 + Redis**，删掉自研代理的维护负担。
3. **记忆引擎接口化，接 Graphiti 做 PoC**（前提：先接一个真实 embeddings 模型）。
4. **接 Langfuse**（OTel 导出即可），给质量看板和风险回溯补上 LLM 级数据。
5. **K8s 生产化时**一起上：agent-sandbox / Kueue（叠加）/ KEDA / SPIRE / OpenFGA / cert-manager / Kata。

## 来源

- 记忆：[Cognee 对比（厂商自写）](https://www.cognee.ai/blog/guides/open-source-memory-frameworks-llm-agents)、[Hindsight 自托管记忆系统对比](https://hindsight.vectorize.io/blog/2026/08/11/open-source-agent-memory-systems)、[Agent 记忆系统评述](https://rickhigh.substack.com/p/eight-agent-memory-systems-none-of)、[多租户记忆隔离](https://supermemory.ai/blog/multi-tenant-memory-noisy-neighbor-isolation/)、[记忆作用域设计](https://www.mintmcp.com/blog/scopes-agent-memory)、[腾讯 Team Memory 报道](https://venturebeat.com/data/tencents-team-memory-shares-ai-agent-memory-across-a-team-with-no-governance-yet-for-when-its-wrong)、[Caura](https://enterprisedna.co/directories/open-source/caura)、[KD6](https://awesome.ecosyste.ms/projects/github.com%2Fdevigned%2Fkd6)
- 沙箱：[自托管沙箱对比（Helix）](https://helix.ml/blog/self-hosted-agent-sandboxes)、[Northflank：E2B 自托管替代](https://northflank.com/blog/self-hostable-alternatives-to-e2b-for-ai-agents)、[Beam：自托管代码沙箱](https://www.beam.cloud/blog/how-to-self-host-code-sandbox)、[LogRocket 对比](https://blog.logrocket.com/comparing-ai-agent-sandbox-platforms-e2b-modal-daytona-and-more/)
- MCP 网关：[agentgateway](https://agentgateway.dev/)、[Docker MCP Gateway](https://www.docker.com/blog/docker-mcp-gateway-secure-infrastructure-for-agentic-ai/)、[IBM ContextForge](https://github.com/dad-io/mcp-context-forge)、[AWS：MCP 网关与注册中心](https://aws.amazon.com/blogs/opensource/governing-ai-assets-at-scale-with-mcp-gateway-and-registry/)、[Arcade：注册中心/网关/运行时](https://arcade.dev/blog/mcp-gateways-runtimes-registries-guide)
- 模型网关：[Envoy AI Gateway 替代对比](https://openalternative.co/alternatives/envoy-ai-gateway)、[API7 AI 网关对比](https://api7.ai/ai-gateway-comparison)、[Geekflare 开源 LLM 网关](https://geekflare.com/open-source/llm-gateway/)、[Maxim：LiteLLM 替代（厂商自写）](https://www.getmaxim.ai/articles/top-5-litellm-alternatives-in-2026/)
- 观测与评测：[MLflow：自托管评测工具对比](https://mlflow.org/articles/tags/llm-evaluation-tools-compared/)、[Lyzr：开源 LLM 观测平台](https://www.lyzr.ai/blog/open-source-llm-observability-platforms/)、[Latitude：Agent 观测选型](https://www.latitude.so/blog/ai-agent-observability-platforms-buyers-guide-2026)
- 知识库：[Onyx：自托管 RAG](https://onyx.app/insights/self-hosted-rag)、[Atlan：企业 RAG 平台对比](https://atlan.com/know/enterprise-rag-platforms-comparison/)、[Sourcegraph 替代](https://getunblocked.com/blog/sourcegraph-alternatives/)、[PipesHub](https://dev.co/ai/rag/pipeshub-ai)
