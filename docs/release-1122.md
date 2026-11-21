# SuperCloudAgents 发布说明（2026-11-22）

本次发布覆盖 `128dfc5` 之后至 `407a0c5` 合入的 Phase 6 功能。升级已有部署时，运行
`alembic upgrade head`，再重启 API 与 Worker。`0023_task_messages` 保存插话记录，
`0024_projects` 将已有顶层任务关联到各租户的「默认项目」。以下需登录的 API 使用租户令牌。

## 新功能与用法

### 项目与线程

用租户令牌登录 Portal，从侧栏的「项目」进入 `/portal#/projects`，选择「新建项目」保存目标、
仓库与基线、通用指令和默认后端与模型。在项目内点「新线程」提交任务，每条线程独立执行和审核。
项目页按状态归组线程，并汇集文件、记忆和用量。设置弹窗支持 Esc 或点击背景关闭。

用 `POST /v1/projects` 提交 `{"name":"结算服务"}`，再把返回的 ID 作为 `POST /v1/tasks`
的顶层 `project_id`。省略该字段时任务归入「默认项目」。用 `GET /v1/projects/{id}/threads`
查询线程，可按 `group=ready_for_review` 筛选待审核项。项目文件与用量分别通过
`GET /v1/projects/{id}/library` 和 `GET /v1/projects/{id}/usage` 查询。
默认设置的修改只影响新线程。在「设置」暂停或归档项目后，运行中的线程在安全检查点暂停，
新线程提交返回 409。也可用 `POST /v1/projects/{id}/pause` 暂停。
[项目与线程操作](projects.md)

### 执行图 DAG

在 Portal「所有线程」打开任务，查看「执行图 DAG」。图默认从上往下排列，折叠主任务、子任务和
Best-of-N 候选，点击节点可展开工具调用、验证和修复过程。可切换横向布局，实时刷新时会保留展开与
缩放状态。升级后，进行中的子任务继续使用原工作区。若图组件未加载，仍可查看任务详情和已发布补丁的影响。
[执行图操作](dag.md) · [补丁影响](decouple.md#查询补丁影响)

`GET /v1/tasks/{id}/dag` 返回完整的 `nodes` 和 `edges`。图可读取已归档的事件，旧任务缺少的
时间或用量字段为 `null`。在已启动的部署上，用 `python scripts/dag_demo.py --repo /path/to/repo`
生成一条脚本化任务。脚本需要有提交、插话和审核权限的 `SCA_API_TOKEN`，以及 Worker 可读取的临时 Git 仓库。
[演示步骤](dag.md#运行脚本化演示)

### 运行中插话

在 Portal 打开任务，在「插话」输入框补充要求。默认的 `queue` 把消息放在下一轮指令开头。
勾选「立即打断当前轮」使用 `interrupt`，停止单 Agent 当前轮，保存已完成的编辑后续跑。
源码模式下也会停止该命令的进程组。审核中发送插话会发起修改并重新提交审核。

用 `POST /v1/tasks/{id}/messages` 发送 `{"text":"请给计算函数补充文档字符串。","mode":"queue"}`。
用同一路径的 `GET` 查看 `pending`、`delivered` 或 `applied` 状态。每条消息最多 4000 字符，
终态任务拒绝新消息并返回 409。并行子任务和 Best-of-N 期间的消息在合并或选定候选后交给主任务。
[插话操作](operations.md#运行中插话)

### DeepSeek Harness 后端

在 Docker 模式用 `make portal-setup` 准备 DSH 镜像与 LiteLLM 网关，配置模型后，在 Portal
「提交任务」选择 DeepSeek Harness，模型填 `deepseek-flash` 或 `deepseek-v4-pro`。
用 `POST /v1/tasks` 提交时，在 `spec` 里填 `agent_backend: "dsh"` 和 `model: "deepseek-flash"`，
省略 `image`。工单触发可在 Portal「集成」的默认规格中选用同一后端与模型，或在
`/v1/integrations/scm` 的请求体 `default_spec` 中设置。DSH 使用网关按轮签发的虚拟密钥，
支持续接会话，暂不支持任务的 MCP `tools` 配置。
[DeepSeek Harness 配置与 API 示例](../README.md#使用-deepseek-harness) · [工单配置](scm.md#通过工单触发任务)

### 无 Docker 的源码模式

准备 Git、Python 3.12、Postgres、Node 和 npm，运行 `bash scripts/portal_demo.sh up --mode source`。
打开脚本给出的 `/portal` 地址，登录后可从「提交任务」选择可用的后端。用 `GET /healthz` 检查服务，
登录后用 `GET /v1/portal/config` 查看 `sandbox_driver: "local"` 和 `unavailable_agent_backends`。
向 `POST /v1/tasks` 提交时可在 `spec.repo_url` 中传宿主机上的 Git 仓库路径。
用 `bash scripts/portal_demo.sh down` 停止本机服务。

源码模式以当前用户身份执行任务，无沙箱隔离，容器资源限制和出口白名单不生效。只提交信任的仓库和命令。
Codex 和 Pi 需要宿主机可执行文件。OpenCode 脑手分离、源码构建和冠军固化仍需 Docker。
[源码模式依赖与 API 示例](operations.md#源码模式)

### 只读访客与无密钥启动

Docker 和源码模式均可在未配置 DeepSeek 密钥时启动并展示历史。从回环地址直接打开 `/portal`，
演示环境默认进入 `eval` 租户的「所有线程」。无需令牌即可调用 `GET /v1/tasks`、
`GET /v1/tasks/{id}/dag` 和 `GET /v1/tasks/{id}/messages`。插话发送者在响应中会被遮盖。

访客只可读取配置的单个租户的部分历史，项目、成员、管理和写入接口需要令牌。点击访客横幅上的「登录」
切换身份。演示环境未配置模型时，经认证的任务、评测和演进提交返回 409 `model_not_configured`。
在 Portal 概览以 `eval` 管理员身份保存模型密钥可重启网关。用 `SCA_GUEST_VIEW=off` 关闭访客入口。
[访客配置与访问范围](operations.md#只读访客模式)

### Stripe 模拟器

完成 Portal 初始化和 `0022_payments` 迁移后，先停止运行中的 Portal，再用
`SCA_STRIPE_SIMULATOR=1 bash scripts/portal_demo.sh up` 启用本地模拟器。以新演示租户的管理员身份，
在 Portal「团队与用量 → 订阅与付款」选择套餐，在结账页点「完成付款」。模拟器发送签名 webhook，
可演练订阅、付款失败暂停提交、付款恢复及发票查询。模拟状态保存在内存中，重启后使用新演示租户。

用 `POST /v1/tenant/billing/checkout` 提交 `{"plan":"team"}` 获取模拟结账链接，
向该链接追加 `/complete` 发送 POST，然后用 `GET /v1/tenant/billing` 核对状态。
基础商用烟测可加 `--stripe-sim http://127.0.0.1:12111` 检查模拟付款链路。
[模拟器操作](operations.md#stripe-模拟器) · [烟测命令](commercial-readiness.md#运行烟测)

## 现场验证记录

负责人于 2026-11-22 在一台演示虚拟机上验证。除源码模式一项外，现场演示使用 Docker 模式。

| 验证项 | 现场结果 |
| --- | --- |
| DeepSeek Harness 后端 | `agent_backend: "dsh"`，DSH `0.2.2-rc.5` 经 LiteLLM 使用 `deepseek-flash`。真实编码任务读取文件、编辑并运行测试，平台验证通过，任务为 `SUCCEEDED`。提交时必须省略 `spec.image`，Worker 才会选用 `sca-sandbox-dsh:latest`。上游包声明 Node ≥24.9，镜像使用 Node 22.22.2。本次运行成功，仍需关注兼容性。 |
| 插话 | `queue` 模式下，第 1 轮发送的消息在第 2 轮作为最高优先级指引送达，最终补丁包含要求的文档字符串，消息状态为 `applied`。`interrupt` 模式下，第 1 轮在验证前被打断，第 2 轮带着插话开始并通过验证。 |
| 源码模式 | 在未安装 Docker 的全新 Ubuntu 24.04 容器中，通过 apt 安装 `postgresql`、`python3`、`nodejs` 和 `npm`。`portal_demo.sh up --mode source` 启动 Postgres、Temporal CLI、LiteLLM、API 与 Worker，`/healthz` 正常。无 DeepSeek 密钥可启动，缺少 Node 时给出明确的安装提示。`fake` 编码任务在 `local` 沙箱中运行并达到 `SUCCEEDED`，`down` 停止全部服务。本次未跑真实模型。现场 `codex`、`pi` 和 `opencode-brain` 均报告不可用。 |
| 执行图 DAG | `scripts/dag_demo.py` 用 `fake` 后端生成 58 个节点、86 条边，覆盖 8 个子任务及其依赖、Best-of-3、验证失败后修复、插话、合并和审核。Portal 从上到下显示 21 个折叠后的摘要节点，可展开。 |
| 访客只读 | 从回环地址无令牌打开 Portal，进入 `eval` 租户的只读「所有线程」。任务、执行图和插话记录的 GET 返回 200，插话 `actor` 已遮盖。项目列表、提交、取消、成员与管理路由返回 401。界面未显示写入控件。 |
| 基础商用烟测 | `scripts/commercial_smoke.py --with-agent` 为 14 PASS / 0 FAIL / 1 SKIP。服务未以 Stripe 模拟器重启，`stripe-checkout` 跳过。模拟器本身有集成测试覆盖。 |
| Pi 后端 | 按用户要求，本轮暂缓复测。此前失败很可能与 DSH 一样由 `spec.image` 覆盖后端镜像引起，下次省略该字段重测。 |
| 测试套件 | 负责人记录单元测试约 990 项通过，集成测试 289 项通过。 |

## 已知限制与后续工作

- 项目页面首屏加载性能由 PERF 继续优化。
- 真实 Stripe 验收需要客户提供测试 API 密钥、webhook 签名密钥和套餐 Price ID。 [Stripe 配置](operations.md#stripe-订阅与付款)
- 长程（longhaul）演进实测未观察到相对基线的提升。
