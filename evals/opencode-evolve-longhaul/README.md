# OpenCode 演进基准 · 长程功能开发

本套件有 16 道功能工单，开发集（`dev`）11 道，留出集（`holdout`）5 道。
用例使用 Python 3.12 和标准库，可以离线运行。每轮上限 900 秒，公开验证失败后
允许一次修复，关闭项目记忆。`deepseek-flash` 的目标通过率为 30–60%，即开发集
4–6/11、留出集约 2/5。

## 用例

四个应用共用离线工作台框架。每题有独立的起始仓库，包含 3,815–3,864 行 Python
（含回归测试，公开验证脚本另计）、110 项既有测试和版本一的历史数据。参考实现每题
改动 10–11 个文件，涵盖业务模块、CLI/API 注册、版本化存储、权限、双语消息、
历史迁移、报表、操作指南和测试。高难度用例还要求新操作写入审计。

| 应用 | 开发集 | 留出集 |
| --- | --- | --- |
| Workyard 工具借用 | `reservation-handoffs`、`maintenance-windows`、`pool-substitutions` | `return-disputes` |
| Framehouse 演出排期 | `rehearsal-calls`、`performance-notices`、`venue-trades` | `attendance-corrections` |
| Groveboard 市场采购 | `purchase-rounds`、`shortfall-credits`、`crate-cycles` | `agreement-rollovers` |
| Watershed 水文巡检 | `sampling-routes`、`calibration-campaigns` | `incident-handoffs`、`sample-holds` |

公开命令 `python -B verify.py` 跑回归及一条两步的成功路径。隐藏验收在独立工作区
重跑公开命令，并检查所有工单条款，包括跨记录变更的原子性、日期报表、CLI/API 一致性、
权限和审计。迁移检查涵盖重新编号的历史记录、先使用功能再运行迁移、重复运行及冲突回滚。
冻结的版本一读取器和便携导出仍能读取升级后的工作区。隐藏脚本的时限为 33 秒。

## 本地自检

从 SCA 项目根目录运行。完整参考仓库位于 Git 忽略的
`.data/evolve-longhaul-solutions/<case-id>/`。每题还有两个 `partial-*/` 子目录，
分别缺少历史迁移注册，以及报表接入或经理权限约束。自检在临时副本中编译、运行并核对
预期的失败阶段。在独立工作树中可通过 `LONGHAUL_SOLUTIONS_DIR` 指向已有参考目录。

```bash
export PYTHONPATH="$PWD/src"
V=/home/ubuntu/projects/SuperCLoudAgent/.venv/bin
timeout 240 "$V/python" -B evals/opencode-evolve-longhaul/check_cases.py \
  "${LONGHAUL_SOLUTIONS_DIR:-.data/evolve-longhaul-solutions}"
```

16/16 起始仓库通过既有回归并被验收拒绝，16/16 参考实现通过隐藏验收，32/32 部分
实现通过公开验证并在预期的隐藏检查失败。

## 校准记录

2026-11-21，OpenCode `2.3.0-sca.8` 与 `deepseek-flash` 在原开发集的现场运行
`973314dc` 中，公开验证通过 11/11，隐藏验收通过 1/11（`maintenance-windows`）。
每题用一轮，平均花费 0.075 美元。

第一轮校准（HARD4B）简化了 7 道开发题和 3 道留出题，将部分新操作的审计设为可选，
部分报表的待处理列表按当前状态统计，并明确了快照字段、报表键和业务错误码。用当时
的验收回放 `973314dc` 保存的补丁，公开验证通过 11/11，隐藏验收通过 5/11
（`maintenance-windows`、`performance-notices`、`purchase-rounds`、`rehearsal-calls`、
`reservation-handoffs`）。

2026-11-21，HARD4B 合并后的现场运行 `1a3bad75` 使用相同的 OpenCode 版本和模型，
开发集隐藏验收通过 2/11（`calibration-campaigns`、`rehearsal-calls`）。补丁回放衡量
了已有提交对新验收的兼容性。两次现场运行的通过用例不同，期望通过率需用多次独立运行估计。

第二轮校准（HARD4C）明确了 5 道开发题（`maintenance-windows`、`reservation-handoffs`、
`pool-substitutions`、`performance-notices`、`purchase-rounds`）和 2 道留出题
（`attendance-corrections`、`return-disputes`）的命令注册、权限、双语消息、报表和升级约定。
工单明确了升级回执与 `store.upgraded` 审计事件，以及各题何时跳过已导入的记录，何时由共享
升级器拒绝同 ID 冲突。演出改期请求未改变日期和场地、采购批次混合供应商或市场等业务冲突
明确返回 `409`。

本轮目标是新运行开发集约 5/11、留出集约 2/5，实际通过率待现场验证。

## 平台运行

将目录放到 `SCA_EVAL_SUITES_DIR` 下（默认为 `evals/`）。用有提交权限的 `eval`
租户令牌发起开发集基线评测。

```bash
curl -fsS -X POST "${SCA_API_URL}/v1/evals/runs" \
  -H "Authorization: Bearer ${SCA_EVAL_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "suite": "opencode-evolve-longhaul",
    "split": "dev",
    "variant": {
      "agent_backend": "opencode",
      "agent_version": "2.3.0-sca.8",
      "model": "deepseek-flash"
    },
    "repeat": 1,
    "concurrency": 1,
    "budget": 8,
    "case_budget": 0.50
  }'
```

用响应中的 ID 查询 `GET /v1/evals/runs/<run_id>`，记录逐题的隐藏验收、轮次、
成本及套件哈希。固定版本、模型、预算和哈希后比较结果。候选确定后以相同参数将
`split` 改为 `holdout`，单独验证留出集。
