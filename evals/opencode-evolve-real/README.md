# OpenCode 真实工程演进基准

共 24 题，16 个开发用例（dev）和 8 个留出用例（holdout）。每个类别有 2 道
开发题和 1 道留出题。任务在 `python:3.12` 使用标准库离线运行。每轮上限
900 秒，公开验证失败时允许一次修复，关闭项目记忆。

此前 OpenCode 与 deepseek-flash 在单轮运行中通过了基础层 19/20 道开发题
和困难层 16/16 道开发题。本层希望把同一基线的通过率降到约 30–60%。
该区间是设计目标，尚无本层的真实模型实测数据。

## 类别和难点

| 类别 | 开发题 | 留出题 | 难点 |
| --- | --- | --- | --- |
| language-build | template-renderer、cron-calendar | ini-source-map | 从 `SPEC.md` 实现完整语法和求值流程，同时满足作用域、日期边界或原始错误位置 |
| data-build | json-patch-engine、framed-record-codec | csv-dialect-sampler | 指针移动顺序和深复制、跨任意分块的校验顺序、多候选 CSV 的完整解析与评分 |
| stateful-build | quota-buckets、ttl-cache | leased-queue | 在连续操作中保持原子更新、精确时间、回调顺序以及失效租约与旧凭据的边界 |
| scattered-spec | report-columns、shipment-rates | catalog-plan | 从 `docs/`、ADR 和接口 docstring 整合输出、校验和优先级，再落实到调用链 |
| vertical-feature | stock-expiry、stock-relabel | stock-reorder | 三个独立任务从同一 Stockroom 基线出发，需要贯通 CLI、服务、SQLite 和序列化，并保留已有数据与命令 |
| pipeline-debug | snapshot-cutoff、board-cache | subscription-cursor | 从报告堆栈追到时间键、缓存键或扫描层，验收单独调用底层入口检查根因 |
| state-debug | inventory-transfers、sensor-window | batch-retry-checkpoint | 用跨仓、跨传感器或重放数据复现问题，修复数据归属、合并顺序或持久化格式 |
| summary-verification | workspace-ignore、timeline-compaction | invoice-allocation | 公开命令只打印失败数和总数，需要用独立探针定位祖先规则、区间叠加或分摊余数 |

九道组件题只把 `SPEC.md` 的入口给 Agent。隐藏验收用固定数据和种子生成
60–200 组检查，覆盖边界、无效输入和多步状态。Stockroom 的每个起始仓库有
3,300 多行、52 个文件以及 112 项现有 `unittest`。公开验证先跑完整回归，再运行
新功能的冒烟检查。隐藏验收也用真实版本 1 的 SQLite 文件验证升级、重开和跨层输出。
这些 SQLite 文件属于题目，SCA 平台无需数据库迁移。

## 本地自检

从项目根目录运行：

```bash
export PYTHONPATH="$PWD/src"
V=/home/ubuntu/projects/SuperCLoudAgent/.venv/bin
timeout 150 "$V/python" -B evals/opencode-evolve-real/check_cases.py .data/evolve-real-solutions
```

参考解按 case ID 保存在 Git 忽略的 `.data/evolve-real-solutions/<case-id>/`，每个目录是
完整参考仓库。自检在临时副本中编译 Python 文件，注入 `acceptance/hidden.py`，
逐题验证起始仓库未通过而参考仓库通过。Stockroom 起始版本还必须先通过完整旧回归，
才算预期失败。每次隐藏验收会核对题面声明保护的文件，再运行原公开验证和独立检查。
隐藏脚本运行上限 25 秒，自检子进程外层上限 27 秒。原始用例目录应保持无缓存，
它的文件内容参与套件哈希。

本地结果为 **24 题通过自检**。每题的起始仓库均被拒绝，参考仓库均通过验收。

## 平台运行

使用属于 `eval` 租户且有提交权限的令牌。下面的请求对开发集运行一次，使用
平台配置的 OpenCode 默认版本。需要复现某个版本时填写 `variant.agent_version`。

```bash
curl -fsS -X POST "${SCA_API_URL}/v1/evals/runs" \
  -H "Authorization: Bearer ${SCA_EVAL_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "suite": "opencode-evolve-real",
    "split": "dev",
    "variant": {"agent_backend": "opencode", "model": "deepseek-flash"},
    "repeat": 1,
    "concurrency": 1,
    "budget": 2,
    "case_budget": 0.10
  }'
```

使用返回的运行 ID 查询 `GET /v1/evals/runs/<run_id>`，查看通过数、轮次和耗费。
候选确定后用同一请求将 `split` 改为 `holdout`。做配对比较时固定模型、OpenCode
版本、预算和套件哈希，候选之间只改变待评测的提示词或工具配置。

## 现场校准（2026-11-20）

OpenCode 2.3.0-sca.8 + deepseek-flash，开发集 16 题全部通过隐藏验收，每题均 1 轮，
单题费用 $0.004–$0.074（合计约 $0.37）。该层对此模型过易，演进主要依靠成本信号
（同等通过率下开发集费用至少降低 15%）。
