# Windows 无 WSL 启动 Portal

把下面这段交给 Windows 上的 Agent。

```text
请在这台 Windows 上用 Python 3 和 PowerShell 启动 SuperCloudAgents 的只读 Portal。当前机器不能安装 WSL。使用仓库里的公开归档浏览各个模块，全程只读取已有记录。

仓库 https://github.com/phao1/SuperCloudAgents
分支 supercloudagent1122

1. 将 supercloudagent1122 更新到最新提交。已有仓库时保留本地修改。首次拉取可运行 git clone --branch supercloudagent1122 https://github.com/phao1/SuperCloudAgents.git。
2. 如果旧版 serve_portal_history.py 正在运行，先在它的 PowerShell 窗口按 Ctrl+C。进入仓库根目录，运行 py -3 scripts/serve_portal_history.py。若 Python 命令是 python，使用 python scripts/serve_portal_history.py。端口被其他程序使用时，可加 --port 8001。
3. 用同一端口访问 /healthz，确认 portal 字段为 archive，task_progress 字段为 true。如果字段缺失，检查分支和旧进程是否已更新。
4. 保持服务运行，打开脚本打印的 /portal 地址。进入「任务进度」，选择 T010，检查「架构进度」「执行泳道」和「播放回放」。打开「共享记忆」，点击「T010 · 事件 #61」，确认回到该任务的记忆命中证据。
5. 打开「任务历史」确认有 64 条任务和 2,869 条执行事件，打开「评测中心」确认可以查看系统提示词基线的用例结果。左侧也应有「项目」「自动演进」「工具与知识库」「集成」等入口。
6. 如果没有 Python，可用浏览器直接打开 Milestone/history/task-history.html 查看任务历史。各模块的只读 Portal 需要上述 Python 服务。最后告诉我分支、提交号、页面地址和验证结果。
```

仓库提供匿名任务、3 条公开评测记录与 1 条已公开的共享记忆正文。其余工作空间记忆、成员、配额与私有知识库，可以通过有权限的在线 Portal 查看。

- [Windows 上的 Python](https://docs.python.org/3/using/windows.html)
- [完整 Portal 启动说明](../../README.md#portal-快速启动)
