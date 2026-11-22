# Windows 无 WSL 启动 Portal

把下面这段交给 Windows 上的 Agent。

```text
请在这台 Windows 上用 Python 3 和 PowerShell 启动 SuperCloudAgents 的只读 Portal。当前机器不能安装 WSL。使用仓库里已有的公开归档，查看任务、评测和版本记录。

仓库 https://github.com/phao1/SuperCloudAgents
分支 supercloudagent1122

1. 将 supercloudagent1122 更新到最新提交。已有仓库时保留本地修改。首次拉取可运行 git clone --branch supercloudagent1122 https://github.com/phao1/SuperCloudAgents.git。
2. 如果旧版 serve_portal_history.py 正在运行，先在它的 PowerShell 窗口按 Ctrl+C。进入仓库根目录，运行 py -3 scripts/serve_portal_history.py。若 Python 命令是 python，使用 python scripts/serve_portal_history.py。端口被其他程序使用时，可加 --port 8001。
3. 保持服务运行，打开脚本打印的 /portal 地址。检查默认页是「概览」，并依次打开「评测中心」「任务历史」「版本对比」。确认评测中心能点开系统提示词基线的用例结果，任务历史显示 64 条任务和 2,869 条执行事件。
4. 用同一端口访问 /healthz，确认 portal 字段为 archive。如果仍只看到任务历史，确认已更新仓库并重启旧进程，再检查浏览器打开的是新的 /portal 地址。
5. 如果没有 Python，可以用浏览器直接打开 Milestone/history/task-history.html 查看离线任务历史。最后告诉我分支、提交号、页面地址和验证结果。
```

只读 Portal 随仓库提供 3 条公开评测记录和原有的任务归档。Mac 或 Linux 上运行的完整 Portal 可通过 SSH 端口转发访问。

- [Windows 上的 Python](https://docs.python.org/3/using/windows.html)
- [完整 Portal 启动说明](../../README.md#portal-快速启动)
