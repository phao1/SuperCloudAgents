# Windows 启动与历史回放提示词

把下面这段复制给 Windows 上能执行终端命令的 Codex。

```text
请在这台 Windows 电脑上启动 SuperCloudAgents 的历史回放 Portal。当前机器不能安装 WSL。我要只读查看已有执行历史，不创建、重跑或恢复任务。

仓库 https://github.com/phao1/SuperCloudAgents
分支 supercloudagent1122

1. 在 Windows PowerShell 里拉取该分支的最新提交。已有仓库就复用，保留本地修改。首次拉取执行：
   git clone --branch supercloudagent1122 https://github.com/phao1/SuperCloudAgents.git
   cd SuperCloudAgents

2. 本机已有 Python 3 时，在仓库根目录执行：
   py -3 scripts/serve_portal_history.py
   如果 Python 命令是 python，则执行 python scripts/serve_portal_history.py。保持服务运行，打开 http://127.0.0.1:8000/portal。如果 8000 已被占用，可加 --port 8001，并打开脚本打印的地址。请求同一端口的 /healthz，确认 mode 为 history。

3. 如果没装 Python，直接用 Edge 或 Chrome 打开 Milestone/history/task-history.html。如果还没拉取仓库，从下面的 GitHub 文件页点「Download raw file」，下载后用浏览器打开。
   https://github.com/phao1/SuperCloudAgents/blob/supercloudagent1122/Milestone/history/task-history.html

4. 确认页面显示 64 条任务和 2,869 条执行事件，点开一条任务检查时间线。最后告诉我分支、提交号、页面地址或文件位置，以及检查结果。
```

本地服务只需要 Python 3，展示仓库里的历史归档。完整 Portal 可以在 Mac 或 Linux 上启动，再由 Windows 通过 SSH 端口转发访问。

- [Windows 上的 Python](https://docs.python.org/3/using/windows.html)
- [完整 Portal 启动说明](../../README.md#portal-快速启动)
