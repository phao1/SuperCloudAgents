# Windows 源码模式

在 Windows PowerShell 5.1 或 PowerShell 7 中，从源码启动 Portal、Worker、PostgreSQL、
Temporal 和 LiteLLM。默认 Agent 为 DeepSeek Harness（`dsh`），默认模型为
`deepseek-flash`。Docker 和 WSL 均无需安装。

如需由 Windows 上的编码 Agent 完成安装和验证，可直接复制启动提示词。[Windows Agent 启动提示词](windows-agent-start.md)

## 安装依赖

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id OpenJS.NodeJS.LTS
```

安装后打开新的 PowerShell。DeepSeek Harness 需要 Node.js 24.9 或更新的 LTS。
首次启动会创建 `.venv`，下载校验过的 Temporal CLI，并使用 npm 把 `@deepseek-ai/dsh`
装入 `.data/tools/dsh`。可在 `.env` 中设置 `SCA_DSH_VERSION`，启动时会安装指定版本。
升级后再次运行 `up` 会为已有的 DSH 安装补跑本地子进程和终端所需的 npm 脚本。
源码模式仅运行 `@deepseek-ai/dsh-subprocess-local`、`koffi` 和 `node-pty` 的安装脚本。
[DSH CLI 依赖](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.5/apps/cli/package.json#L186-L188)

启动脚本会复用现有的 PostgreSQL 工具，或者下载并校验 EDB 的 Windows x64 二进制包。
它在 `.data/pg` 初始化独立的数据目录，优先使用本机 5432 端口，端口已被占用时使用空闲端口。
脚本为当前 Windows 用户设置 PostgreSQL 数据目录和相关文件的权限，供 `pg_ctl` 以受限令牌启动。
数据库仅监听 `127.0.0.1`，使用 SCRAM 密码认证。
如果下载失败，可安装 PostgreSQL 并指定其工具目录。若提示缺少 VC++ 运行库，可安装对应的运行库。
[PostgreSQL 官方 Windows 安装说明](https://www.postgresql.org/download/windows/)

```powershell
winget install -e --id PostgreSQL.PostgreSQL.17
$env:SCA_POSTGRES_BIN = 'C:\Program Files\PostgreSQL\17\bin'
winget install -e --id Microsoft.VCRedist.2015+.x64
```

## 首次启动与登录

在仓库根目录运行。首次安装需要联网。普通用户和以管理员身份打开的 PowerShell 使用相同命令。

```powershell
.\scripts\portal_source.ps1 up
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/healthz'
```

打开 <http://127.0.0.1:8000/portal>。默认本机免令牌，以 `eval` 管理员身份进入首页。
在「设置 → 运行环境」可以查看当前运行模式。Windows 版仅支持源码模式，Docker 选项显示原因。
在「设置 → 访问与安全」打开「需要访问令牌」，随后用 `.data/portal.env` 中的
`SCA_EVAL_API_TOKEN` 登录。在本机 PowerShell 私下运行以下命令查看令牌。

```powershell
Select-String SCA_EVAL_API_TOKEN .\.data\portal.env
```

也可以在首启前通过 `PORTAL_TOKEN` 提供自选令牌，仅允许字母、数字、下划线和连字符。
以下命令会隐藏输入。[访问开关与优先级](operations.md#本机-portal-访问令牌)

```powershell
$env:PORTAL_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Portal 登录令牌' -AsSecureString)).Password
.\scripts\portal_source.ps1 up
Remove-Item Env:PORTAL_TOKEN
```

在「设置 → 模型」的 DeepSeek 卡片填写 API Key 并保存，Portal 会重启本机 LiteLLM 网关。
也可以在启动前将密钥填入仓库根目录的 `.env`；脚本会复用现有配置。在获取密钥前可以
查看历史记录。请妥善保存 `.env` 和 `.data/portal.env`。

连接 Ollama、LM Studio 或 vLLM 时，在「设置 → 模型 → 本地模型」选择服务，填写基础 URL 和模型 ID。
同机服务可以使用 `127.0.0.1`，免密服务的 API Key 可留空。保存后测试连接，再按需选择「默认模型」。
[模型接入与 API 示例](models.md)

## 导入 Linux 历史

在 Linux Docker 模式服务器的 SCA 仓库根目录导出历史。

```bash
bash scripts/portal_demo.sh export-history sca-history.zip
```

将 ZIP 复制到 Windows 的 SCA 仓库根目录，在 PowerShell 中运行。

```powershell
.\scripts\portal_source.ps1 import-history .\sca-history.zip
```

脚本会启动或复用本机 PostgreSQL、应用迁移并导入历史。导入后用 `up` 启动 Portal 查看记录。
Windows 本机可用 `export-history <zip>` 导出。[历史归档内容与高级用法](history-transfer.md)

## 运行任务与演进

1. 在「首页」或「任务」点击「新建任务」，填写任务指令，例如「创建 Flask 商店，包含商品列表和购物车」，
   并选择 DeepSeek Harness。新项目可填绝对输出目录，例如 `C:/work/flask-shop`。留空则生成到
   `~/sca-projects/<项目名>-<短 ID>`。任务默认先自动规划。已有仓库的任务可填写仓库地址和基线分支。
   准备和验证命令由 Git Bash 执行，请使用相对于任务工作目录的路径。在「冠军覆盖」选择
   「使用当前冠军」可应用同一 DSH 版本和模型的冠军指引。
2. 提交后进入任务详情，在「执行图 DAG」和「对话」查看进展。需要人工确认时，在对话顶部
   审阅规划或任务结果。批准后，输出目录会出现代码和提交。勾选「完成后无需人工审核，直接写入」
   可在验证通过后自动交付。路径可从任务标题下复制，写入记录可在对话和 DAG 的「结果」阶段查看。
3. 在首页点击「开始演进」，或打开「自动演进 → 演进记录」。选择 DeepSeek Harness、评测套件、模型和预算，再提交运行。
   首次建议用内置套件 `dsh-smoke` 和 `deepseek-flash`，按下方参数试跑预计花费 ¥1–2。
   基线版本留空时使用 `SCA_DSH_VERSION`。打开演进记录查看执行图与评测结果。
   启动脚本会应用 DSH 演进所需的迁移 `0025`。

### 对话与待审批

「对话」按 Agent 和子任务显示回复，命令、工具和文件变更可按轮展开。任务插话可选择
「排队」或「打断」，演进指引用于下一代提议，回执显示在对应消息下方。点击「定位节点」
可打开关联的 DAG 节点。

侧栏「待审批 (N)」汇总当前租户可查看的规划、结果、修复、候选晋升和采集发布审批。
点击事项可跳到对应卡片。任务审核可填写修改意见，批准结果时可勾选需要启用的共享记忆。
演进晋升与采集发布沿用各自的管理员权限。只读用户可查看有权访问的审批卡片。

### API 提交示例

PowerShell 脚本发起写请求时继续使用 `SCA_EVAL_API_TOKEN`。免令牌开启时，本机的普通 GET
查询可以省略令牌，例如 `Invoke-RestMethod http://127.0.0.1:8000/v1/tasks`。

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

用同一令牌新建 Flask 商店。第一条请求写到自选目录，第二条写到默认目录。

```powershell
$api = 'http://127.0.0.1:8000'
$spec = @{
    instruction = '用 Flask 创建商店，包含商品列表、购物车、测试和启动说明。入口为 app.py'
    agent_backend = 'dsh'
    model = 'deepseek-flash'
    output_dir = 'C:/work/flask-shop'
    plan = 'auto'
    verify_command = 'python -m compileall -q app.py'
    auto_finish = $true
}

function Submit-Shop {
    $body = @{ kind = 'coding'; spec = $spec } | ConvertTo-Json -Depth 4
    Invoke-RestMethod -Method Post -Uri "$api/v1/tasks" -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
}

$chosen = Submit-Shop
$spec.Remove('output_dir')
$default = Submit-Shop
Invoke-RestMethod -Uri "$api/v1/tasks/$($default.id)" -Headers $headers
```

使用同一租户管理员令牌提交 `dsh-smoke` 演进并查看执行图。

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
$inbox = Invoke-RestMethod -Uri "$api/v1/approvals" -Headers $headers
$inbox.items | Select-Object kind, title, href
Remove-Variable token, headers
```

任务审批使用 `POST /v1/tasks/{id}/review`，例如提交
`{"decision":"request_changes","feedback":"请补充空输入测试"}`。演进候选使用
`POST /v1/evolutions/{id}/approve` 或 `/reject`，请求体提供 `candidate_id`。

## 状态、日志和停止

```powershell
.\scripts\portal_source.ps1 status
.\scripts\portal_source.ps1 logs postgres
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
  `logs postgres` 同时显示 `.data/run/postgres.log` 的初始化及 `pg_ctl` 输出和
  `.data/run/postgres-server.log` 的数据库进程输出。工具版本需与 `.data/pg/PG_VERSION` 匹配。
- 网关启动失败时查看 `.data/run/litellm.log`。若 Portal 提示密钥未配置，在「设置 → 模型」保存密钥后重试。
  `restart-gateway` 可加载从 `.env` 手工更新的密钥。
- 源码模式可选 OpenCode、Codex 或 Pi。用 `SCA_AGENT_BACKEND` 选择默认后端。
  OpenCode 首次选用时下载本机发布包。Codex 需要将 `codex.exe` 加入 `PATH`，
  Pi 可通过 `npm install -g @earendil-works/pi-coding-agent@1.5.2` 安装。
  OpenCode 脑手分离和构建固化需要 Docker。
- 使用源码模式烟测时，在当前 PowerShell 中提供 `PORTAL_TOKEN`，然后运行
  `.\.venv\Scripts\python.exe scripts/source_smoke.py`。烟测使用 `fake` 后端，
  检查 Portal HTML、文件补丁和验证结果。免令牌开启时，烟测从同一 Origin 无令牌提交任务。
  全程无需模型密钥。

### 源码模式持续集成

在 GitHub Actions 的 **Source mode** 工作流中选择 **Run workflow**，可在 Windows、macOS 和
Ubuntu 上验证源码启动。依赖安装成功后，即使单测失败也会运行烟测。运行失败时，在该次运行的
**Artifacts** 下载对应系统的 `source-smoke-<系统>`，查看经过脱敏的服务日志。
