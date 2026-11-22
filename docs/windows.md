# Windows 源码模式

在 Windows PowerShell 5.1 或 PowerShell 7 中，从源码启动 Portal、Worker、PostgreSQL、
Temporal 和 LiteLLM。默认 Agent 为 DeepSeek Harness（`dsh`），默认模型为
`deepseek-flash`。Docker 和 WSL 均无需安装。

## 安装依赖

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id OpenJS.NodeJS.LTS
```

安装后打开新的 PowerShell。DeepSeek Harness 需要 Node.js 24.9 或更新的 LTS。
首次启动会创建 `.venv`，下载校验过的 Temporal CLI，并使用 npm 把 `@deepseek-ai/dsh`
装入 `.data/tools/dsh`。可在 `.env` 中设置 `SCA_DSH_VERSION`，启动时会安装指定版本。
[DSH CLI 依赖](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.5/apps/cli/package.json#L186-L188)

启动脚本会复用现有的 PostgreSQL 工具，或者下载并校验 EDB 的 Windows x64 二进制包。
它在 `.data/pg` 初始化独立的数据目录，优先使用本机 5432 端口，端口已被占用时使用空闲端口。
如果下载失败，可安装 PostgreSQL 并指定其工具目录。若提示缺少 VC++ 运行库，可安装对应的运行库。
[PostgreSQL 官方 Windows 安装说明](https://www.postgresql.org/download/windows/)

```powershell
winget install -e --id PostgreSQL.PostgreSQL.17
$env:SCA_POSTGRES_BIN = 'C:\Program Files\PostgreSQL\17\bin'
winget install -e --id Microsoft.VCRedist.2015+.x64
```

## 首次启动与登录

在仓库根目录运行。首次安装需要联网，可用普通用户启动。

```powershell
.\scripts\portal_source.ps1 up
```

打开 <http://127.0.0.1:8000/portal>，点击右上角「登录」。初次启动生成的令牌保存在
`.data/portal.env` 的 `SCA_EVAL_API_TOKEN` 中，可在本机私下查看。也可以在首启前通过
`PORTAL_TOKEN` 提供自选令牌，仅允许字母、数字、下划线和连字符。以下命令会隐藏输入。

```powershell
$env:PORTAL_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Portal 登录令牌' -AsSecureString)).Password
.\scripts\portal_source.ps1 up
Remove-Item Env:PORTAL_TOKEN
```

登录后在「概览」填写 DeepSeek API Key 并保存，Portal 会重启本机 LiteLLM 网关。
也可以在启动前将密钥填入仓库根目录的 `.env`；脚本会复用现有配置。在获取密钥前可以
查看历史记录。请妥善保存 `.env` 和 `.data/portal.env`。

## 运行任务与演进

1. 在「提交任务」选择「DeepSeek Harness」。填写仓库地址、基线分支、任务要求和验证命令。
   仓库可使用本机绝对路径，例如 `C:/work/demo`。平台的准备和验证命令由 Git Bash
   执行，使用相对于任务工作目录的路径，例如 `python -m pytest -q`。在「冠军覆盖」选择
   「使用当前冠军」，可应用同一 DSH 版本和模型的冠军指引。
2. 提交后进入任务详情，在「执行图 DAG」和执行泳道查看进展。验证通过后，可在「任务审核」
   检查补丁并选择批准或继续修改。
3. 在「自动演进」选择 DeepSeek Harness、包含开发集和留出集的套件、模型和预算，点击
   「开始演进」。基线版本留空时使用 `SCA_DSH_VERSION`。打开演进记录查看执行图与评测结果。
   启动脚本会应用 DSH 演进所需的迁移 `0025`。[准备评测套件](design-evolution.md#最小试跑)

### API 提交示例

```powershell
$token = [System.Net.NetworkCredential]::new('', (Read-Host 'Portal 登录令牌' -AsSecureString)).Password
$headers = @{ Authorization = "Bearer $token" }
$task = @{
    kind = 'coding'
    spec = @{
        repo_url = 'C:/work/demo'
        base_ref = 'main'
        instruction = '修复计算函数并补充边界测试'
        verify_command = 'python -m pytest -q'
        agent_backend = 'dsh'
        model = 'deepseek-flash'
        use_champion = $true
    }
} | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/tasks' -Headers $headers `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($task))
```

准备好 `dsh-smoke` 套件后，使用同一租户管理员令牌提交演进并查看执行图。

```powershell
$api = 'http://127.0.0.1:8000'
$body = @{
    suite = 'dsh-smoke'
    agent_backend = 'dsh'
    model = 'deepseek-flash'
    generations = 1
    candidates = 2
    repeat = 1
    concurrency = 1
    budget_usd = 0.20
    case_budget = 0.02
} | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "$api/v1/evolutions" -Headers $headers `
    -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri "$api/v1/evolutions/$($run.id)/dag" -Headers $headers
Remove-Variable token, headers
```

## 状态、日志和停止

```powershell
.\scripts\portal_source.ps1 status
.\scripts\portal_source.ps1 logs worker
.\scripts\portal_source.ps1 down
```

`logs` 省略服务名时显示各服务最近的日志。PID 和日志保存在 `.data/run/`。
`down` 停止本机服务并保留数据库和历史记录。

### 运行限制与排查

- **源码模式没有 Docker 隔离。** Agent 和验证脚本以当前用户身份直接在宿主机运行，
  每个任务的工作目录位于 `.data/sandboxes/`。容器镜像、容器资源限制与网络出口白名单
  在此模式下不生效。请使用可信仓库和命令。
- Git Bash 找不到时，检查 Git for Windows 是否在 `PATH` 中。可以设置
  `$env:SCA_BASH = 'C:\Program Files\Git\bin\bash.exe'`。DSH 自带的 Windows 工具
  由 Harness 使用 PowerShell 执行，平台的 `sca-fake`、准备和验证脚本使用 Bash。
- 端口 7233、4000 或 8000 被占用时，先停止占用端口的服务，再运行 `up`。
  PostgreSQL 初始化失败时查看 `.data/run/postgres.log`，并确认工具版本与
  `.data/pg/PG_VERSION` 匹配。
- 网关启动失败时查看 `.data/run/litellm.log`。若 Portal 提示密钥未配置，使用同一个
  登录令牌在「概览」保存密钥后重试。`restart-gateway` 可加载从 `.env` 手工更新的密钥。
- 源码模式可选 OpenCode、Codex 或 Pi。用 `SCA_AGENT_BACKEND` 选择默认后端。
  OpenCode 首次选用时下载本机发布包。Codex 需要将 `codex.exe` 加入 `PATH`，
  Pi 可通过 `npm install -g @earendil-works/pi-coding-agent@1.5.2` 安装。
  OpenCode 脑手分离和构建固化需要 Docker。
- 使用源码模式烟测时，在当前 PowerShell 中提供 `PORTAL_TOKEN`，然后运行
  `.\.venv\Scripts\python.exe scripts/source_smoke.py`。烟测使用 `fake` 后端，
  检查 Portal HTML、文件补丁和验证结果，无需模型密钥。
