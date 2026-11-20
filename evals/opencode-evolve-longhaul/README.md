# OpenCode 演进基准 · 长程功能开发

本套件有 16 道功能工单，开发集（`dev`）11 道，留出集（`holdout`）5 道。
用例使用 Python 3.12 和标准库，可以离线运行。每轮上限 900 秒，公开验证失败后
允许一次修复，关闭项目记忆。`deepseek-flash` 的目标通过率为 30–60%，真实
OpenCode 现场校准待负责人完成。

## 用例

四个应用共用离线工作台框架。每题有独立的起始仓库，包含 3,815–3,864 行 Python
（含回归测试，公开验证脚本另计）、110 项既有测试和版本一的历史数据。参考实现每题
改动 11 个文件，涵盖业务模块、CLI/API 注册、版本化存储、权限、双语消息、审计、
历史迁移、报表及新增的操作指南和测试。

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
预期的失败阶段。请在保留参考目录的原工作树运行自检。

```bash
export PYTHONPATH="$PWD/src"
V=/home/ubuntu/projects/SuperCLoudAgent/.venv/bin
timeout 240 "$V/python" -B evals/opencode-evolve-longhaul/check_cases.py .data/evolve-longhaul-solutions
```

16/16 起始仓库通过既有回归并被验收拒绝，16/16 参考实现通过隐藏验收，32/32 部分
实现通过公开验证并在预期的隐藏检查失败。

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
