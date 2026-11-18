# 已跑通的场景与网页操作指引

截至 2026-11-18，代码快照为 `a5a0d63`，全量回归结果为 711 passed、48 skipped、0 failed。
本文分三部分：

- 第一部分列出真实跑通过的场景，以及每个场景用到了平台的哪些功能。
- 第二部分是在 Windows 电脑上从零启动项目，写成 agent 可以逐步执行的形式。
- 第三部分是网页操作指引，照着点就能在 Portal 上把主流程完整跑一遍。

“跑通”的判定标准：在真实平台上跑过（真实 Temporal、Postgres、Docker 沙箱、网关，模型是 deepseek-flash），
并且留有运行 ID。只有单元测试或基准测试的项目会单独注明。
每一项的细节都在 `docs/design-phase4.md` 对应章节的“负责人现场验证”部分。

---

## 一、已跑通的场景

### 场景 1：编码任务主流程（一期到三期）

提交任务后，平台依次执行：克隆仓库、在沙箱里运行 Agent、运行校验命令，然后进入人工审核；
审核时可以要求修改，最后合并或推送。详细记录见 [main-flow-and-next.md](main-flow-and-next.md)。

用到的功能：

- 通用内核与 Temporal 持久工作流
- 可插拔 Agent 后端（Codex、OpenCode、假 Agent）
- Docker 沙箱，出口受限
- 按任务签发的临时网关密钥
- 多租户与行级安全（RLS）
- 并行校验、审核和修改轮次
- 空闲时释放沙箱，有新工作时自动重建
- GitHub App 推送：已在真实 App 上验证

### 场景 2：Portal 闭环（评测 → 定位 → 修源码 → 重新编译 → 回归）

这是本项目的主演示，详细过程见 [portal-demo.md](portal-demo.md)。

| 环节 | 运行 ID | 结果 |
|---|---|---|
| 基线评测（`2.3.0-sca.1`，内含植入的缺陷） | `ed36ecb9` | 5/6 通过，失败题是 chinese-greeting |
| 修复任务（OpenCode 修改 OpenCode 自己的源码） | `98432901` | 删掉了出问题的正则，并补了回归测试 |
| 断网编译 | `4e1eb230` | 得到 `2.3.0-sca.4` |
| 用新版本复测 | `dc49829e` | 6/6，通过率 83% → 100%，结论为“建议采纳” |

用到的功能：

- 评测集与评测运行
- 执行泳道和证据面板
- 证据规则，例如“非 ASCII 字符丢失”
- 修复任务及其来源标签
- 审批
- 断网、限内存的构建容器
- 版本谱系与版本对比

### 场景 3：在一台全新机器上一键搭建

在一份全新的拷贝里执行 `make portal-setup`，从零完成以下步骤：

1. 起数据库、Temporal 和网关；
2. 构建沙箱镜像和 TLS 出口代理镜像；
3. 下载 OpenCode 并植入缺陷；
4. 编译基线版本；
5. 打印 Portal 地址。

然后在这份拷贝上把场景 2 走了一遍。

### 场景 4：四期并行与脑手分离（全部默认关闭，按需打开）

| 能力 | 跑通的证据 | 用到的功能 |
|---|---|---|
| P4.1 脑手分离（`opencode-brain`） | 修复版 6/6；沙箱断网且没有任何密钥 | OpenCode 在沙箱外运行，工具调用通过 WorkspaceDriver 转进 `--network=none` 的容器执行 |
| P4.1b 常驻 brain 池 | 每轮开销 p50 从 2612 ms 降到 357 ms；评测结果不变 | 一个服务端进程同时驱动多个会话 |
| K 轨迹检查 | Agent 绕开坏掉的工具后，验收虽然通过，仍被判为失败 | 过程证据规则 |
| P4.3 overlay 工作区 | Portal 闭环 `867435e6` → `20fd5709`，83% → 100%；每个任务工作区 86 KB | Docker overlay 写时复制 |
| pi 后端 | `c822cb6a` 6/6，每个用例 17–39 秒 | 第二个开源 Agent 后端，有独立的沙箱镜像 |
| P4.5 准入与网关限流 | `bc9c9caf` 6/6，45 次调用全部经过限流代理，上游 0 次 429 | 排队准入、加权公平调度、令牌桶限流、网关反压 |
| P4.2 空闲冻结 | `3da076b2` 6/6，结果不受影响 | `docker pause`/`unpause`。结论：只省 CPU，不省内存，所以保持默认关闭 |
| P4.4 快照分叉 | 同样的任务提交两次：第 2 次 `051793ca` 直接跳过 setup，分叉耗时 0.07 秒 | 按租户隔离的工作区快照 |
| P4.6 handd（仅基准测试） | 单条命令延迟 p50 从 60 ms 降到 2–8 ms | 沙箱内常驻执行守护进程。尚未接入 brain |

### 场景 5：安全出口

| 验证项 | 结果 |
|---|---|
| 域前置修复 | 白名单外的域名，用 SNI 或 Host 前置都会被拒；缺失 SNI 的请求和直连 IP 也会被拒 |
| Docker 下的测试 | 通过 |
| kind + Calico 两节点集群 | 23 passed |
| 多副本 Worker 共享 CA 用的 RWX 存储卷 | 未验证 |

### 还没跑通或没做的

- 稳定性维度：按你的要求暂缓。
- handd 接入 brain driver。
- Best-of-N。
- 一键 setup 部署限流代理。
- 多副本 Worker 共享 CA 的 RWX 存储卷。
- 真实 embeddings 模型和告警渠道：需要你提供。

---

## 二、在 Windows 电脑上从零启动（可交给 agent 逐步执行）

> 给执行这一节的 agent：按顺序做，每步都有“检查”，检查不过就停下来报告，不要跳步。
> **不要读取、打印或提交 `.env` 里的任何密钥**；`DEEPSEEK_API_KEY` 由用户自己填写。
> 所有命令都在 **WSL2 的 Ubuntu 终端**里执行，不要在 PowerShell / CMD 里跑项目命令。

### 1. 准备环境（Windows 侧，只做一次）

| 要求 | 说明 |
|---|---|
| Windows 10 22H2+ 或 Windows 11，x86_64 | ARM 版 Windows 不支持（镜像和 OpenCode 构建都是 amd64） |
| WSL2 + Ubuntu 24.04 | PowerShell（管理员）：`wsl --install -d Ubuntu-24.04`，装完重启并设置 Linux 用户名密码 |
| Docker Desktop | 设置里勾选 “Use the WSL 2 based engine”，并在 Resources → WSL integration 里打开 Ubuntu-24.04 |
| 内存 | 建议给 WSL 至少 8 GB：在 `%UserProfile%\.wslconfig` 写入下面内容，然后在 PowerShell 执行 `wsl --shutdown` 再重开 Ubuntu |
| 磁盘 | WSL 所在盘至少 20 GB 空闲（setup 自身要求 15 GB） |

```ini
[wsl2]
memory=8GB
processors=4
```

检查（在 Ubuntu 终端）：

```bash
docker info --format '{{.ServerVersion}}'   # 能打印版本号
docker compose version                        # 能打印版本号
free -g | sed -n 2p                           # total 最好 ≥ 7
df -h ~ | tail -1                             # Avail ≥ 20G
```

`docker info` 报权限错误时：确认 Docker Desktop 已启动且 WSL integration 已打开，然后关闭并重开 Ubuntu 终端。

### 2. 安装依赖（Ubuntu 终端）

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git make curl
python3.12 --version                          # 检查：Python 3.12.x
```

### 3. 克隆仓库（必须放在 WSL 的 Linux 文件系统里）

```bash
cd ~
git clone https://github.com/phao1/SuperCloudAgents.git
cd SuperCloudAgents
git log --oneline -1                          # 检查：应为 93a0c55 或更新
```

**不要**克隆到 `/mnt/c/...`（Windows 盘）：那里文件权限、换行符和 Docker 挂载都会出问题，速度也慢很多。
也不要在 Windows 侧用 Git for Windows 克隆后再拷进来（会把脚本换行符改成 CRLF）。

### 4. 第一次 setup：生成 `.env` 后会停下

```bash
make portal-setup
```

第一次会生成 `.env` 并提示“请在 .env 中填写 DEEPSEEK_API_KEY”，然后退出。这是预期行为。

**这一步由用户本人完成**：用 `nano .env` 打开，把 `DEEPSEEK_API_KEY=REPLACE_ME` 改成自己的密钥后保存。
agent 不要代填，也不要 `cat .env`。

### 5. 第二次 setup：真正安装（约 10–20 分钟）

```bash
PORTAL_TOKEN=my-portal-token make portal-setup
```

`PORTAL_TOKEN` 可选，用来指定一个好记的登录令牌（只能是字母、数字、下划线、连字符）；不指定就随机生成，
结束时会打印出来。setup 依次会：安装 Python 依赖 → 拉起 Postgres / Temporal / LiteLLM 网关 → 数据库迁移 →
构建沙箱镜像、pi 镜像、TLS 出口代理镜像 → 从 GitHub 下载 OpenCode v2.3.0 和约 3–4 GB 依赖 →
植入缺陷 → 编译基线 `2.3.0-sca.1` → 启动 API 和 worker。

检查：最后几行应是

```text
Portal: http://127.0.0.1:8000/portal
登录令牌: <你的令牌>
```

再确认：

```bash
make portal-status        # 服务都在、网关可达、版本列表里有 2.3.0-sca.1
```

### 6. 在 Windows 浏览器里打开

WSL2 默认会把 `localhost` 转发给 Windows，直接在 Windows 的浏览器打开 <http://localhost:8000/portal>。

打不开时：在 `%UserProfile%\.wslconfig` 的 `[wsl2]` 下加一行 `networkingMode=mirrored`，PowerShell 执行
`wsl --shutdown`，重开 Ubuntu 后执行 `make portal-up`，再试。

### 7. 日常启停与排错

```bash
make portal-up            # 启动（会打印网址和令牌）
make portal-down          # 停止 API 和 worker
bash scripts/portal_demo.sh down --all   # 连同数据库/Temporal/网关一起停（数据保留）
make portal-status        # 状态
bash scripts/portal_demo.sh logs         # API 和 worker 最近日志
```

| 现象 | 处理 |
|---|---|
| setup 报“Docker 不可用” | 启动 Docker Desktop，确认 WSL integration 已开，重开终端 |
| setup 报“需要至少 15 GB 可用磁盘” | 清理磁盘（`docker system prune` 会删除未使用的镜像，先确认再执行） |
| 下载 OpenCode 或依赖超时 | 网络问题，重跑 `make portal-setup`（可重复执行，已完成的步骤会跳过） |
| 构建时 WSL 卡死或被杀 | `.wslconfig` 的 memory 调大到 8 GB 以上；OpenCode 构建本身限制在 2.5 GB |
| 评测任务全部失败 | `bash scripts/portal_demo.sh logs` 看 worker 日志；多半是 `.env` 里的密钥不对或网关不通 |

## 三、在网页上把流程跑一遍

### 0. 登录

打开 Portal（Windows 上是 <http://localhost:8000/portal>；如果用的是原来那台 Linux 服务器，先在自己电脑上
`ssh -L 8000:127.0.0.1:8000 <服务器>` 再打开同一地址），在“连接工作空间”里填 setup / `make portal-up`
打印的**登录令牌**（原服务器上是 `portal-token`），点“连接”。

内存 8 GB 以下的机器请一次只跑一个评测，并发设 1–2。

### ① 基线评测（约 2–3 分钟）

“评测中心”：套件 `opencode-demo`，后端 **OpenCode**，OpenCode 版本选 **`2.3.0-sca.1`**（带缺陷的基线），
模型 deepseek-flash，重复次数 1，并发 2，其他默认。点“开始评测”。

预期：运行详情显示 **5/6**，矩阵里 chinese-greeting 是红的。
（偶尔 agent 会绕开坏掉的工具让验收通过，这时“过程检查”仍会把这题判为失败，照样可以继续。）

### ② 定位

点 chinese-greeting 那一格进入“执行轨迹”，在泳道里点“文件编辑”。右侧证据面板会显示两行中文问候语被清空，
规则标出“非 ASCII 字符丢失”。

### ③ 发起修复（约 2–5 分钟）

“修复闭环”：问题诊断写一句（例如“edit/write 工具写文件时丢失中文字符”），修复后端 **OpenCode**，
修复模型 deepseek-flash，点“发起修复”。可以点“查看修复轨迹”看 agent 干活。
任务进入待审核后，“批准修复”按钮才会亮起。

### ④ 批准并构建（约 30 秒–1 分钟）

点“批准修复”，再点“构建新版本”。构建日志实时滚动，结束后得到新版本：
**新机器上是 `2.3.0-sca.2`**（原服务器上已有 sca.2–sca.6，会是 `2.3.0-sca.7`）。

### ⑤ 回归（约 2–3 分钟）

点“用新版本重跑评测”。完成后跳到“版本对比”：结论卡“建议采纳”，通过率 83% → 100%，修复 1 题、回归 0 题；
“构建与源码记录”里能看到新版本由哪个修复任务产生。

### 可选：比较 pi 和 OpenCode

“评测中心”把后端改选 **pi**，其余不变，开始评测。预期 6/6，每题 17–39 秒。之后可在“版本对比”里和上面的运行并排看。

### 可选：打开四期功能

这些是环境开关，不在网页上切换。在 `.data/portal.env` 末尾追加需要的行，然后 `make portal-down && make portal-up`：

```bash
export SCA_WORKSPACE_OVERLAY=true     # overlay 写时复制工作区（Docker Desktop 上未验证）
export SCA_WORKSPACE_SNAPSHOTS=true   # 快照分叉（重复次数 > 1 时自动启用）
export SCA_SANDBOX_HANDD=true         # 沙箱内常驻执行守护进程
```

限流代理需要额外启动 `sca-gateway-limiter` 容器，步骤见 `docs/design-phase4.md` 的 P4.5b，一键 setup 不包含。
要恢复默认，删掉追加的行，再 `make portal-down && make portal-up`。

### 已知未验证

本指引的 Windows 部分按 setup 脚本的要求编写，**还没有在真实 Windows + WSL2 + Docker Desktop 上跑过**；
已验证的是 Linux 上的全新拷贝一键 setup。如果遇到问题，把出错命令和 `bash scripts/portal_demo.sh logs`
的输出发给我。
