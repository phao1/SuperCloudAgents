# OpenCode 演进基准

30 个标准库 Python 编码任务，全部离线运行；每个仓库使用 `python:3.12`。
10 个类别各有 2 个 dev、1 个 holdout：bug-fix、feature、refactor、multi-file、
test-writing、boundary、unicode、large-file、search、iteration。

公开 `repo/verify.py` 是必要条件，隐藏验收会再次运行它并核对其内容未被修改，
再检查需求中的边界条件。测试编写题会在正确实现及三个错误变体上运行提交的
`unittest`；大文件题检查目标行外的字节保持不变。iteration 类的公开检查包含
多个失败路径，用于观察验证与修正行为；实际是否需要第二轮由模型决定。

本地自检（不用 Docker）：

```bash
timeout 180 .venv/bin/python evals/opencode-evolve/check_cases.py .data/evolve-solutions
```

参数目录中每个子目录按 case ID 命名，包含完整参考仓库。参考实现只放在被忽略的
`.data/evolve-solutions/`，不进入套件或提交。脚本在临时副本中逐题检查：未修改仓库的
隐藏验收失败、参考仓库的隐藏验收通过；每次运行有超时。请勿在用例原目录执行
导入或生成缓存文件；套件哈希覆盖所有文件，包括 holdout、验收与本文。

API 启动请求增加 `"split": "dev"` 或 `"split": "holdout"`；省略则运行所有题。
旧套件不标 split 时仍按原方式运行，显式请求不存在的 split 会报错。
拆分不会改变套件哈希。运行记录的 `variant.split` 与 `variant.case_metadata`
保存请求与用例元数据快照；API 详情的结果行从快照提供 `split`、`category`，
无需迁移数据库，也不依赖之后修改过的套件。直接读取 Store 的调用方应从该快照
取得元数据（数据库 `eval_results` 表未增加列）。

`variant.agent_overlay` 接受字典并原样传入任务规格；具体覆盖字段及安全限制由
任务规格层验证。

难度目标是小模型约 40–70% 通过率；这里尚未运行真实模型或 Docker，不能将该目标
视为实测结果。负责人需用固定基线分别跑 dev 与 holdout，之后再决定是否调整难度。
