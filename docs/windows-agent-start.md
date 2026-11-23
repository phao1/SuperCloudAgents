# Windows 源码启动提示词

把下面这段交给 Windows 上的 Agent。

```text
请在这台 Windows 电脑上从源码启动 SuperCloudAgents，接入我选择的模型，并验证 DeepSeek Harness 能完成一个小任务。
全程使用原生 PowerShell（5.1 或 7），禁止使用 Docker 和 WSL。源码模式下，Agent 和验证命令以当前用户身份直接在宿主机运行，无隔离。请使用可信仓库和命令。

仓库 https://github.com/phao1/SuperCloudAgents
分支 supercloudagent1122

1. 检查 Python 3.12、Git for Windows、Node.js LTS（至少 24.9）和 npm。按需安装。
   winget install -e --id Python.Python.3.12
   winget install -e --id Git.Git
   winget install -e --id OpenJS.NodeJS.LTS
   如果 Node.js LTS 已安装但版本过低，用 winget upgrade -e --id OpenJS.NodeJS.LTS 更新。
   安装或更新后打开新的 PowerShell，再用 py -3.12 --version（没有 py 时用 python --version）、git --version、node --version 和 npm.cmd --version 确认版本。

2. 首次拉取时运行 git clone --branch supercloudagent1122 https://github.com/phao1/SuperCloudAgents.git。
   已有对应仓库且平台正在运行时，先在仓库根目录运行 .\scripts\portal_source.ps1 down 停止旧版本服务，数据库和历史会保留。
   已有对应仓库时先运行 git status --short 和 git fetch origin supercloudagent1122，再用 git switch supercloudagent1122 切换分支。
   只有远程分支时用 git switch --track origin/supercloudagent1122。切换后运行 git merge --ff-only FETCH_HEAD。
   如果更新有冲突、分支已分叉或切换会覆盖本地改动，保留原工作区，在另一个目录克隆。
   进入仓库根目录并确认提交包含 scripts\portal_source.ps1。若缺少脚本，报告当前提交，等分支发布源码启动脚本后再继续。
   若旧的 serve_portal_history.py 正在占用 8000，先在启动它的终端按 Ctrl+C。没有原终端时，先确认监听进程确实是该脚本，再按 PID 停止它。

3. 在仓库根目录运行 .\scripts\portal_source.ps1 up，等待退出并确认成功。
   首次运行会准备 Python 环境和原生依赖，并启动 PostgreSQL、Temporal、LiteLLM、Worker 和 API。
   用 Invoke-RestMethod -Uri 'http://127.0.0.1:8000/healthz' 确认 status 为 ok，报告脚本打印的 Portal 地址 http://127.0.0.1:8000/portal。
   本机浏览器默认免令牌，可以直接使用 Portal。在「设置 → 访问与安全」可开启「需要访问令牌」。若已开启，请我在本机 PowerShell 私下用 Select-String SCA_EVAL_API_TOKEN .\.data\portal.env 查看并登录。
   令牌、模型密钥和包含它们的请求体不得出现在聊天、命令行参数、日志或最终报告中。

4. 先问我选哪种模型接入，等我选择后再配置。
   a. DeepSeek API。请我在「设置 → 模型」的 DeepSeek 卡片私下输入并保存 API Key。内置模型名为 deepseek-flash 和 deepseek-v4-pro。
      通过 API 保存时使用 POST /v1/portal/provider-key，使用隐藏输入提供 key 字段，等保存结果报告网关已重启。
   b. 第三方或本机服务。向我确认接口类型（OpenAI 兼容或 Anthropic 兼容）、基础 URL、API Key（本机免密时可留空）以及上游模型 ID 和对应的平台模型名。
      可按需填写上下文窗口和输入、输出价格（美元 / 百万 Token）。同机 OpenAI 兼容服务的地址形如 http://127.0.0.1:<port>/v1。
      Anthropic 兼容服务填写基址，由网关请求 /v1/messages。在「设置 → 模型」选择供应商预设并填写模型，等 Portal 显示网关已重启。
      使用 API 时通过 POST /v1/portal/providers 保存，收到 202 后用 GET /v1/portal/providers/restarts/{restart.id} 等到 succeeded。状态为 failed 时先检查服务日志。
   用「测试连接」或 POST /v1/portal/providers/test（请求体为 {"model":"平台模型名"}）测试所选模型。随后用 GET /v1/models 核对可用模型。
   本机免令牌时，PowerShell 的 GET 可以省略认证头，例如 (Invoke-RestMethod 'http://127.0.0.1:8000/v1/models').data | Select-Object -ExpandProperty id。
   PowerShell 脚本发起写请求时继续使用 SCA_EVAL_API_TOKEN。已开启「需要访问令牌」时，受保护的 GET 也需要令牌。需要令牌时，请我通过本机隐藏输入提供它，只输出模型 ID。
   $token = [System.Net.NetworkCredential]::new('', (Read-Host 'Portal 登录令牌' -AsSecureString)).Password
   $headers = @{ Authorization = "Bearer $token" }
   (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/v1/models' -Headers $headers).data | Select-Object -ExpandProperty id
   用完 API 后执行 Remove-Variable token, headers。

5. 如果我提供了 sca-history.zip，将其放到仓库的 .data 目录（该目录不会被提交）。运行 .\scripts\portal_source.ps1 import-history .\.data\sca-history.zip，在 Portal 检查导入的任务和「自动演进」中的记录。

6. 用「新项目」流程跑一个多 Agent 冒烟任务。先在 SCA 仓库根目录用 .\.venv\Scripts\python.exe -m pip install pytest 安装验证依赖。
   在「首页」或「任务」点击「新建任务」。新建任务只有「任务指令」必填，仓库地址和分支留空时平台自动新建空仓库。
   - 任务指令：创建两个互不依赖的 Python 模块，各带 pytest 测试：mathutils（clamp(x, lo, hi)、is_even(n)）和 textutils（slugify(s)、word_count(s)）。
   - Agent 后端选 DeepSeek Harness（dsh），模型选第 4 步验证过的平台模型名。
   - 输出目录填一个空闲的绝对路径，例如 C:\sca-demo\utils；留空则写到 %USERPROFILE%\sca-projects\<项目名>。
   - 验证命令填 python -m pytest -q。任务拆分保持默认「自动规划」。任务预算填 0.15。勾选「完成后无需人工审核，直接写入」。
   提交一次。在任务详情确认：
   - 「执行图 DAG」先出现「规划」，再出现按 Agent 分的泳道（如 Agent 1、Agent 2）并行执行，标题行显示 agent 数、子任务数和并发上限。
   - 「对话」按 Agent 标注每个子任务的输出和进度，最后出现「代码已写入 <目录>（提交 …）」。
   - 输出目录里有两个模块和测试文件，git log 显示 SCA Agent 的提交。
   也可通过 GET /v1/tasks/{task_id} 确认 state 为 SUCCEEDED，并读取 result.output_dir 与 result.output_commit。
   未勾选「完成后无需人工审核」时，任务会出现在左侧「待审批」，在对话顶部的审批卡片点「批准」后才写入目录。
   运行中可在对话框输入指引：「排队发送」在当前轮结束后送达，「打断并发送」（Ctrl+Enter）会立即停下当前轮并带着指引重新开始。

7. 向我说明如何从「自动演进 → 演进记录」开始一次运行。选择 DeepSeek Harness 或 OpenCode。选择 OpenCode 时填写已安装的基线版本。
   首次选 dsh-smoke，选已验证的模型，设小参数，例如一代、两个候选、每题一次、并发 1。
   使用 deepseek-flash 时可先设评测总预算 0.20 美元、单题预算 0.02 美元，反思和候选生成另计。
   启动后在「演进记录」打开 DAG，按进展在「对话」里发指引：「下一代生效」在下一代提议时使用，「立即生效」会中止本代剩余评测并马上按指引重新提议。
   展开「运行控制」可使用「暂停」「恢复」「本代结束后停止」「跳过当前候选」「调整剩余预算」。需要人工批准的候选会出现在左侧「待审批」。
   Windows 只支持源码模式，「设置 → 运行环境」中的 Docker 选项不可用属于正常现象。
   未经我确认，禁止启动付费演进。

8. 排查时在仓库根目录运行 .\scripts\portal_source.ps1 status。
   通过 .\scripts\portal_source.ps1 logs worker、.\scripts\portal_source.ps1 logs litellm 或 .\scripts\portal_source.ps1 logs postgres 查看对应服务。
   API、Temporal 和 LiteLLM 分别使用 8000、7233 和 4000。先确认端口占用，再停止对应进程。
   找不到 Git Bash 时，可设置 $env:SCA_BASH = 'C:\Program Files\Git\bin\bash.exe'，按实际位置调整。
   PostgreSQL 的初始化和服务器日志分别位于 .data\run\postgres.log、.data\run\postgres-server.log。
   DSH 安装失败时查看 .\scripts\portal_source.ps1 logs dsh。up 会安装 DSH 并只运行 @deepseek-ai/dsh-subprocess-local、koffi 和 node-pty 所需的 npm 安装脚本。
   修复依赖后重跑 up。需要停止平台时运行 .\scripts\portal_source.ps1 down。

9. 保持验证成功的服务运行。最后报告 git branch --show-current 和 git rev-parse HEAD 的结果、Portal URL、采用的模型接入方式和模型名、历史导入结果（如有）、冒烟任务 ID、实际并发的 Agent 数、输出目录和提交号、验证结果，以及未完成或失败的项目。请勿附带令牌、密钥或配置文件内容。
```

- [Windows 源码模式](windows.md)
- [模型接入与预算](models.md)
- [历史导出与导入](history-transfer.md)
- [执行图与对话](dag.md)
