# a5a0d63 模块审计记录

> 快照日期：2026-11-18；提交：`a5a0d63dfc1e25dfc75ec62a203761478d88e32a`。

按原图节点与 V2 补充逐项核对实现和测试范围；下表说明判定边界，具体 code/tests/docs 路径保存在 data.json，生成页面提供固定提交链接。原有 52 项的状态等级不变，但对应描述、证据和缺口已按四期更新。新增 12 项使用相同定义。

阶段记录冲突按四期负责人后续补记处理：K8s restricted 不再停在 exit 1；Docker 可执行路径、brain 取消、评测预建权限/失败标记均已补齐。未将单测或早期工程师的跳过项计为现场通过。

| 模块 ID | 状态 | 本次核对结论与边界 |
| --- | --- | --- |
| `ui_goal` | `done` | 原场景表单与预算契约保留；Portal 另提供后端/模型/版本选择。 |
| `ui_dispatch` | `done` | API 集成测试覆盖首次启动失败后重试，避免重复创建任务。 |
| `ui_quality` | `partial` | Grafana 通过率来自验证活动计数。未配置编码验证命令的跳过分支也会计为通过。 缺口：稳定性维度与充分重复实验；缺陷 / 覆盖率跨项目聚合。 |
| `ui_confirm` | `done` | 审核期间保留沙箱及槽位，空闲超时才一同释放；修改反馈必要时重新准入。 |
| `ui_approve` | `partial` | 仍按规划/验证状态转人工，未按置信度或风险等级路由。 缺口：按置信度或风险等级路由审批；独立例外审批工作台。 |
| `ui_history` | `partial` | Portal 真实走查已验证失败题定位；时间来源包含事件估算，不等同于精确模型网关遥测。 缺口：任意时点的开发现场重演。 |
| `design_in` | `partial` | 文件输入适配器仍无在线 Figma/工单连接；overlay 仓库相对路径 intent 未获支持。 缺口：Figma API 连接；工单同步与回写。 |
| `design_spec` | `partial` | 仍为规则配置及截图基准，不含 Token 语义或组件库同步。 缺口：Design Token 语义解析；组件库同步。 |
| `design_intent` | `done` | 三类适配器及沙箱检查保留；未宣称 overlay 下仓库相对路径输入已接通。 |
| `verify_gw` | `done` | 独立命令验证与 CI 提交关联保留；四期过程评分另在 eval 层，不能替代命令验证。 |
| `verify_parser` | `partial` | 结构化退出码/日志仍是通用验证核心；Portal 的启发式诊断不等于通用扫描报告根因解析。 缺口：通用扫描报告解析；自动根因 / 风险分析。 |
| `quality_echo` | `partial` | 失败输出驱动同任务修复；Portal 将验收、过程检查、版本差异和回归结论连到具体任务证据。 缺口：回写外部设计 / 工单平台；稳定性维度验收。 |
| `task_entry` | `done` | 认证、角色与 RLS 保留；活跃配额转为执行前准入，提交只限制排队积压。 |
| `planner` | `done` | 仍校验 2–8 个子任务与已跟踪路径重叠；未新增语义依赖推导。 |
| `decouple` | `partial` | 仍不强制执行期的规划路径范围。 缺口：执行期路径边界强制；语义依赖与影响面分析；跨模块契约推导。 |
| `dag` | `adapted` | Temporal 编排边界保留，四期增加持久准入等待，不是通用 DAG 编辑器。 缺口：通用依赖图建模与编辑；V1 自研调度服务。 |
| `checkpoint` | `partial` | 跨 Worker 检查点续跑的历史验证适用于原有传输路径；常驻 brain 的共享 SQLite 尚不支持按任务归档/跨 Worker 迁移，当前需 Worker 亲和。 缺口：任意历史时点的任务分叉；跨节点 overlay 共享存储和恢复验证；常驻 brain 会话跨 Worker 迁移。 |
| `subtask_q` | `adapted` | 子任务依然共享父槽位；全局父任务限额不等于子沙箱限额。 缺口：通用子任务依赖调度器；独立队列服务。 |
| `optim_entry` | `done` | 同任务反馈续接保留；审核超时释放后会先重新申请执行槽位。 |
| `evo_trigger` | `partial` | ADR-18 将自动改进闭环列为当前范围之外。 缺口：无需人工诊断的自动改进触发；自动推广版本。 |
| `sandbox_sched` | `adapted` | 历史多节点用例由宿主机上的 Worker 驱动，使用 nodeSelector 把沙箱 Pod 放在 kind 工作节点。 缺口：独立跨集群调度服务；子沙箱独立限槽。 |
| `sandbox_pool` | `partial` | 仍是固定容量池；四期 CA 指纹用于只认领当前证书配置的 restricted Pod，旧空闲 Pod 会回收。 缺口：池容量随负载伸缩；生产集群时延验证。 |
| `runtime` | `adapted` | ADR-03 和 ADR-05 将完整 Agent 循环保留在沙箱内，后端通过接口替换。 缺口：V1 独立 Runtime 服务；K8s 上真实 Agent 模型网关接通。 |
| `executor` | `adapted` | 内核通过场景实际 backend 取得附加挂载、网络与镜像，修复任务的只读依赖不再丢失。 缺口：V1 独立执行与装配服务。 |
| `recover_gate` | `adapted` | 恢复前检查对象内容的 SHA-256；故障发生在轮次中途时，从上一份已发布状态重做。 缺口：V1 多类检查点与外部副作用核验。 |
| `hand_brain` | `adapted` | P4.1 负责人确认 sca.1 5/6、修复版 6/6；P 小修复补记确认真实容器取消后 sleep 在 10 秒内消失。 缺口：brain 工具路由到 handd；部门工具目录和授权；PTY 通道。 |
| `memory` | `partial` | 仍是可审核短记忆与向量回退；池模式绕过工具缺陷的记忆提议不证明质量改善。 缺口：真实 embeddings 模型链路复验；记忆对任务质量的对比实验。 |
| `identity` | `partial` | brain 临时 key 在沙箱外；其他整体后端仍在沙箱内使用受限临时 key，不是工作负载证书体系。 缺口：Agent 工作负载证书身份；部门级工具授权模型。 |
| `knowledge` | `partial` | 四期未增加企业文档连接、权限同步或引用溯源。 缺口：企业文档连接与分块；文档级权限同步与引用溯源。 |
| `tool_domain` | `planned` | 目标提交仍无部门工具注册与授权产品；多个 Agent 后端不等于部门工具域。 缺口：部门工具注册与版本；按团队授予工具权限。 |
| `audit` | `partial` | 新增准入、分叉及过程评分证据；未引入不可篡改审计导出。 缺口：合规策略与留存管理产品；防篡改审计导出。 |
| `cache_code` | `partial` | B1.6 规定每台 Worker 保有独立仓库缓存。 缺口：跨 Worker 分布式缓存管理；按负载预热与容量伸缩。 |
| `cache_dep` | `partial` | Portal 初期依赖挂载将工作区从 3.3 GB 降到 343 MB；四期 overlay 另以 upper 计量，不能混淆两者统计口径。 缺口：通用包管理器缓存；跨节点依赖分发与命中管理。 |
| `cache_gw` | `partial` | transport 统一挂载或归档上传下载；Docker + mount 可选基础层、overlay 和准备快照。sync/K8s 不启用这些本地路径优化。 缺口：独立动态缓存挂载服务；跨节点依赖缓存管理。 |
| `object_store` | `done` | 本地/S3 与 manifest-last 持久化边界保留，overlay 新增校验过的状态对象。 |
| `vector_store` | `partial` | 真实 pgvector + 确定性假向量历史验证保留；未找到真实 embeddings 验证补记。 缺口：真实 embeddings 服务验证；固定维度近似索引与容量评估。 |
| `repos` | `partial` | 仍只推分支、不自动创建 PR 或合入；OpenCode 版本仓库/构建另列 V2 能力。 缺口：自动创建 / 更新 PR；统一制品及镜像仓库产品。 |
| `k8s` | `partial` | 负责人 2026-11-18 kind + Calico 23 passed、无跳过，包含 TLS 出口。测试使用单 Worker 宿主目录；多副本 RWX CA PVC 未验证。K8s + model gateway 在 factory 仍被拒绝。 缺口：Worker 实际集群部署、RBAC、探针、排空与伸缩；多副本 RWX CA PVC 实测；K8s 真实模型网关闭环；生产 HA 与容量。 |
| `compute` | `partial` | 资源 limits 与 HPA/KEDA 示例边界未扩成生产容量证明；父任务准入不限制所有子沙箱总数。 缺口：GPU 资源调度；Worker 实际伸缩、KEDA 与容量基线。 |
| `kms` | `partial` | B4 记录真实 Gitea + OpenBao 路径通过。2026-11-16 真实私有 App 仅安装在一个私有测试仓库：OpenBao 保存私钥，Worker 自行换取安装令牌，克隆、推送任务分支、审阅通过；令牌未进入事件、捕获日志或 Temporal 历史，测试分支已清理。接入脚本新增 import 命令，先调用 GET /app 核对私钥所属 App。 缺口：自动 CA 轮换 / 通用 KMS 生命周期；多副本 RWX CA 卷验证。 |
| `obs_store` | `done` | 三路观测和本地 webhook 历史结果保留；真实 Slack/邮件仍未验证。 缺口：真实 Slack / 邮件渠道投递验证。 |
| `net_sec` | `partial` | 负责人补记覆盖早期 K8s exit 1 的阻塞；目标提交已恢复 restricted TLS 出口。不能把 23 项 TLS 测试算成 K8s 完整真实模型闭环。 缺口：K8s 真实 Agent 模型网关安全接通；多副本 CA 共享与轮换运维验证；HTTP/2 合并、ECH 等进一步对抗覆盖。 |
| `evo_goal` | `done` | C4 使用 12 个 coding-basic 样本。2026-11-15 真实对比每模型每题 1 次，两个模型均为 12/12，无法区分通过表现。 |
| `evo_eval` | `done` | 四期基线中文题即使绕行后文件验收通过，也因过程规则失败而计为失败；修复版 sca.6 为 6/6。规则是保守启发式，不是文件系统重演或因果证明。 |
| `evo_improve` | `partial` | 已不只是手动修改提示词；83% → 100% 来自受控植入缺陷演示，不证明任意缺陷自动修复能力。 缺口：全自动诊断、筛选及合入改进；更难真实缺陷与重复实验。 |
| `evo_release` | `partial` | Portal 默认禁用闭环按钮，并要求修复任务就绪，防止恢复状态前误构建 main；通用构建 API 仍支持显式 ref 构建。 缺口：自动灰度、自动回滚与推广策略。 |
| `obs_trace` | `done` | OTel 调用链保留；OpenCode 业务工具时间与 brain 模型耗时估计另由事件记录。 |
| `obs_replay` | `partial` | Portal 将模型步骤、工具、文件编辑及验证按时间画泳道，点击事件显示输入/输出/diff；事件深链接定位轨迹规则，Jaeger 保留调用链。 缺口：可执行的现场重演；精确网关侧模型耗时归因。 |
| `obs_cost` | `done` | LiteLLM 负责实际结算，limiter 的 token 估计只用于限流，不是账单。 |
| `obs_risk` | `partial` | Portal 联动工具异常、中文丢失、耗时异常与过程检查，版本对比给出修复/回归数量；运维指标和告警继续由 Grafana 承接。 缺口：人工介入率分析；统一风险因果归因；稳定性维度完成与验收。 |
| `analysis_kind` | `partial` | 仍有脚本化真实沙箱结果；四期没有真实模型分析验证或 Markdown 渲染交付。 缺口：真实模型分析用例验证；报告 Markdown 渲染。 |
| `model_gateway` | `partial` | 负责人真实限流闭环 6/6，45 次调用全过代理。limiter 默认关闭，一键 setup 不部署该容器；不能把假模型 600/600 视为真实容量证明。 缺口：Kubernetes 上真实 Agent 模型网关；真实 embeddings 验证；跨副本共享限流。 |
| `portal_loop` | `partial` | 2026-11-17 首次浏览器走查及 11-18 overlay 走查均有负责人记录；人为植入 CJK 丢失缺陷，单次比较不能证明稳定性。 缺口：稳定性维度尚未完成；全自动诊断与推广。 |
| `opencode_versions` | `done` | SCA_OPENCODE_REPO_URL、SCA_OPENCODE_SRC_CACHE、SCA_OPENCODE_VERSIONS_DIR、SCA_OPENCODE_DEFAULT_VERSION 和 SCA_OPENCODE_BUILD_MEMORY；默认版本根 data_dir/opencode/bin，<V>/opencode、meta.json，builds/<id>.json/.log。构建内存默认 2500m；自动灰度另待实现。 |
| `brain_split` | `done` | P4.1 三项退出条件负责人已验；初版缺少 @opencode/pty 导致 6 题全失败已修复。SCA_OPENCODE_BRAIN_* 控制宿主、Bun、网关与 Docker prefix；不代表 K8s 已支持该模式。 |
| `brain_pool` | `partial` | 真实容器/假模型基准：冷启动 2742 ms，RSS 约 374 MiB；真实模型并发 3 基线 5/6。并发 1 曾 6/6 是绕开缺陷，后由轨迹评分补足。目录 data_dir/opencode/brain-run/<version>/<worker_id>/。 SCA_OPENCODE_BRAIN_POOL=true（默认），CONCURRENCY=3、IDLE_MINUTES=10、WORKER_ID=local（同样加 SCA_OPENCODE_BRAIN_POOL_ 前缀）；同数据目录的 Worker 必须使用不同且稳定的 ID。当前需要 Worker 亲和。 缺口：跨 Worker 会话库导出 / 导入与无亲和恢复；大量会话的长期内存稳定性验证。 |
| `trajectory_checks` | `done` | 负责人 overlay 基线中文题文件验收通过、过程失败；修复版通过。规则只提供可追溯启发式证据，不是完整文件系统回放或稳定性证明。 |
| `admission_control` | `partial` | Postgres 模拟 300 任务完成，不是模型吞吐量证明。负责人修复 CommonSpec priority 接线及审核提前销毁沙箱回归；压力超过 30 秒/缺失视为未饱和。 缺口：子任务独立限槽；生产规模多 Worker 容量验证。 |
| `gateway_limiter` | `partial` | SCA_GATEWAY_LIMITER=false，端口 4100；真实模型 6/6、45 调用、排队 6.4 秒；假模型 600/600、6 次 429 全恢复。SSE/结果不明传输失败不重放，usage 估计不改变账单。 缺口：多进程 / 多副本共享桶；一键 setup 自动部署；真实提供商容量压测。 |
| `workspace_overlay` | `partial` | 负责人真实 Portal overlay 闭环 5/6 → 6/6，评测 upper 86 KB；不能当成完整 task 物理磁盘大小。后续 P 小修复已接 SCA_DOCKER_BIN，不再保留写死 docker 的早期缺口。 缺口：仓库相对路径 intent 接线；sync/K8s overlay 与跨节点验证。 |
| `workspace_forks` | `partial` | data_dir/snapshots/<content-id>/、<preparation-key>.json、<content-id>.refs/、locks/；保留 24h，上限 8 GiB。真实 setup 示例 2.8 → 1.6 秒、fork 0.07 秒；12/12 无 setup 评测没有分叉。 缺口：Best-of-N 聚合、预算与 API；顶层 CodingSpec fork_from_snapshot 字段；完整 upper+base 差分存储。 |
| `pi_backend` | `done` | SCA_PI_VERSION / SCA_PI_SANDBOX_IMAGE；默认 sca-sandbox-pi:latest。Portal 修 OpenCode 的后端选项不提供 pi，因为修复要带 Bun 的镜像；不是脑手分离模式。 |
| `idle_suspend` | `partial` | 8 容器 CPU 0.32–0.38% → 0%，内存始终 60.7 MiB；线上活跃率 0.80–0.97，60 秒 exec 48 / pause 3。冻结省 CPU，不省内存；负责人已修 exec 被全程锁串行化的问题。 缺口：同等内存提升并发的退出条件未达成；当前小评测收益有限。 |
| `handd` | `partial` | 串行 true/cat/rg p50 2.0/7.6/6.2 ms、p99 7–18 ms；docker exec p50 60–61 ms、p99 94–117 ms。并发 4 p50 4.2/21.6/16.7 ms、p99 8–34 ms；CPU 数据仅宿主脚本及子进程，不含 daemon/容器。 缺口：brain driver 尚未接 handd；PTY / 交互 stdin；部署 wheel 不包含仓库级 daemon。 |

## 本次执行记录

- `timeout 30 python3 Milestone/build.py`：成功生成。
- `timeout 60 python3 Milestone/validate.py`：64 模块、50 原图节点通过；210 个唯一证据路径在 checkout 和目标提交中存在。
- `timeout 30 python3 Milestone/build.py --check`：通过。
- `timeout 60 .venv/bin/ruff check src tests`：All checks passed；最终 `timeout 30 .venv/bin/ruff check Milestone/validate.py src tests` 同样通过。
- `timeout 30 node --check Milestone/app.js` 与 `timeout 30 git diff --check`：通过。
- 另以目标提交核对阶段/导航证据，并逐字节确认源 SVG 与 portal-demo.md 未改；原 52 项状态等级保持不变。
- `timeout 900 .venv/bin/pytest tests/unit tests/integration/test_store.py tests/integration/test_api.py tests/integration/test_workflow.py -q -p no:cacheprovider --basetemp=/tmp/sca-x-milestone-baseline`：**493 passed、1 skipped，99.34 秒**。使用独立临时目录以避开 handoff 记录的共享目录归属问题。
- 额外 Ruff 检查发现未修改的 `Milestone/build.py` 存量 EXE001 / FURB167；`validate.py` 的 import 排序已调整并单独复查。构建脚本仍按任务要求用 Python 解释器运行。

本次不重跑历史现场实验。没有 Docker、bun install、OpenCode 构建或整套 e2e；无可用浏览器/Playwright/Selenium，浏览器交互未复验。原 SVG 哈希由校验器核对。无需负责人修改平台文件；上述功能缺口是目标提交的边界，不是本次新增要求。
