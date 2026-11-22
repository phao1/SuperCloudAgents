# Windows 启动与历史回放提示词

把下面这段复制给 Windows 上能执行终端命令的 Codex。

```text
请在这台 Windows 电脑上启动 SuperCloudAgents，并打开仓库里已发布的执行历史。只做启动和只读验证，不创建、重跑或恢复任何任务，不需要配置模型 API Key。

仓库 https://github.com/phao1/SuperCloudAgents
分支 supercloudagent1122

1. 检查本机环境并拉取该分支的最新提交。优先复用已有的 SuperCloudAgents checkout，保留本地修改。全新安装时可以在 WSL 的 Linux 文件系统中执行 git clone --branch supercloudagent1122 https://github.com/phao1/SuperCloudAgents.git。
2. 先让我看到历史。找到 Milestone/history/task-history.html，必要时从 WSL 复制到 Windows 的下载目录，然后用 Edge 或 Chrome 打开。确认页面有 64 条任务、2,869 条执行事件，点开一条任务检查时间线。如果还不能拉取仓库，先从下面的 GitHub 文件页下载原始 HTML 再打开。
   https://github.com/phao1/SuperCloudAgents/blob/supercloudagent1122/Milestone/history/task-history.html
3. 再启动项目。用 wsl -l -v 检查 WSL 2。没有 Ubuntu 时按需安装 Ubuntu 24.04。在 Ubuntu 中检查 Git、Python 3.12（含 venv）和 Docker Compose，确认 Docker Desktop 的 WSL 集成可用。首次安装在仓库根目录执行 bash scripts/portal_demo.sh setup --mode docker。已经完成安装时执行 bash scripts/portal_demo.sh up --mode docker。Docker 不可用且 WSL 正常时，可以按 docs/operations.md 的源码模式要求准备依赖，再执行 bash scripts/portal_demo.sh up --mode source。
4. 用 HTTP 请求检查 http://127.0.0.1:8000/healthz，成功后在 Windows 浏览器打开 http://127.0.0.1:8000/portal。已发布的记录在上面的离线回放页，新机器的 Portal 连接自己的本地数据库。
5. 最后给我实际分支和提交号、Portal 地址、历史回放页位置，以及两项各自的验证结果。若系统权限、WSL 或安装依赖阻塞 Portal，先把历史回放打开，再说明具体阻塞。
```

- [SuperCloudAgents 启动说明](../../README.md#portal-快速启动)
- [SuperCloudAgents 源码模式](../../docs/operations.md#源码模式)
- [Microsoft WSL 安装说明](https://learn.microsoft.com/windows/wsl/install)
- [Docker Desktop 的 WSL 集成说明](https://docs.docker.com/desktop/features/wsl/)
