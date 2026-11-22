# 历史导出与导入

用一个 ZIP 把 Linux 服务器上的历史带到 Windows 源码实例。在目标 Portal 可查看项目与线程、
任务 DAG、插话、评测结果、演进候选与冠军，以及已发布的补丁和日志。已归档的事件也可回放。
导出与导入均可在 API 和 Worker 运行期间执行。

## Linux 导出

在服务器的 SCA 源码目录中，用运行 Portal 的数据库和对象存储。以下命令适用于 Docker 模式
默认的本机 Postgres 端口和本地对象存储。自定义部署时，先在当前 shell 配置实际的
`SCA_DATABASE_URL` 和对象存储参数。命令使用运维数据库角色，配置从环境变量读取。

```bash
export PYTHONPATH="$PWD/src"
export SCA_DATA_DIR="$PWD/.data"
export SCA_OBJECT_STORE=local
.venv/bin/python -m sca.history export --out "$PWD/sca-history.zip"
```

S3 部署使用 `SCA_OBJECT_STORE=s3`，并在当前环境提供 Worker 使用的 `SCA_S3_*` 配置。
指定租户和 UTC 时间范围的示例如下。日期也可写成带时区的 ISO 8601 时间。

```bash
.venv/bin/python -m sca.history export --out "$PWD/eval-history.zip" \
  --tenant eval --tenant my-team --since 2026-11-01
```

省略 `--tenant` 会导出全部租户。`--since` 按任务和项目的更新时间、评测与演进的最新时间
以及冠军的创建时间选取记录。选中的任务保留完整事件和制品。导出会补齐关联的项目、评测用例、
候选、冠军谱系和数据库中可查到的同租户 DAG 关系。
若任务正在重写同名制品，命令可能提示制品已更改，可在该轮发布后重试。

## Windows 导入

将 ZIP 复制到 Windows 的 SCA 源码目录。运行本机 Postgres，在 PowerShell 中使用目标 Portal
的数据库连接串和数据目录。连接串使用 `postgresql+asyncpg` 驱动，连接到 SCA 数据库。
下例通过隐藏输入读取连接串，适用于 Windows PowerShell 5.1 和 PowerShell 7。

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:SCA_DATA_DIR = Join-Path (Get-Location) '.data'
$env:SCA_OBJECT_STORE = 'local'
$DbUrl = Read-Host '输入本机 SCA 数据库连接串' -AsSecureString
$env:SCA_DATABASE_URL = [System.Net.NetworkCredential]::new('', $DbUrl).Password
& .\.venv\Scripts\alembic.exe upgrade head
& .\.venv\Scripts\python.exe -m sca.history import .\sca-history.zip
```

两端数据库至少应用迁移 `0024`。目标数据库的 Alembic 历史须包含归档记录的版本。导入校验
清单中的文件 SHA-256 和记录数，并在同一数据库中互斥执行。任务、项目、消息、评测、演进以及
冠军的 ID 会保留。任务事件保留原任务内序号，数据库内部流水号由目标库分配。

导入中的未结束任务落为 `CANCELLED`，评测和演进落为 `stopped`，原因均为
`imported while running`。重新导入同一 ZIP 不会更新已有的任务或追加它们的事件。历史冠军
以未启用状态显示，源端启用状态留在 `metrics.history_source_active`。源端默认项目标记留在
`settings.history_source_default`，本机仍按自己的默认项目接收新任务。

## 在 Portal 或 API 中查看

用目标实例上属于对应租户的本机令牌打开 `http://127.0.0.1:8000/portal`。任务详情显示「导入」。
评测和演进沿用 `eval` 租户权限。以下 PowerShell 示例从当前环境读取目标实例的令牌。

```powershell
$Api = 'http://127.0.0.1:8000'
$Headers = @{ Authorization = "Bearer $env:SCA_API_TOKEN" }
Invoke-RestMethod "$Api/v1/projects" -Headers $Headers
$Tasks = Invoke-RestMethod "$Api/v1/tasks" -Headers $Headers
$Tasks
$TaskId = ($Tasks | Select-Object -First 1).id
Invoke-RestMethod "$Api/v1/tasks/$TaskId/dag" -Headers $Headers
Invoke-RestMethod "$Api/v1/tasks/$TaskId/messages" -Headers $Headers
Invoke-RestMethod "$Api/v1/evals/runs" -Headers $Headers
Invoke-RestMethod "$Api/v1/evolutions" -Headers $Headers
```

归档包含历史展示所需的数据和已发布制品。租户凭证与成员、计费与支付记录、LiteLLM 数据库、
演进定时计划、冠军灰度配置、任务输入和运行检查点由目标实例独立管理。导出会过滤凭据字段并
脱敏可识别的密钥。超长文本行和补丁中的二进制差异以占位文本保存。历史指令、代码和制品仍可能
包含业务敏感信息，请在受控位置保存和传输 ZIP。
