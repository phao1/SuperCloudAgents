# OpenCode 演进基准 · 专家层

本套件有 20 题，其中开发集（dev）14 题，留出集（holdout）6 题。每道题在
`python:3.12` 中离线运行，只使用 Python 标准库。每轮上限 900 秒，公开验证
失败时允许一次修复，关闭项目记忆。

2026-11-20，OpenCode `2.3.0-sca.8` 配合 `deepseek-flash` 在上一层
`opencode-evolve-real` 的 16 道开发题中全部首轮通过。本层的基线通过率设计
目标是 30–60%，真实平台和模型校准待完成。

## 用例

每个工程有五个独立的起始仓库，分别带有短工单、既有实现、操作手册、变更记录和
回归测试。工单描述用户观察到的现象。边界行为由仓库中的文档和接口约定给出。
每个起始仓库有 36–53 个文件、1,507–3,009 行 Python（不含公开验证脚本）
和 58–112 项既有回归测试。

| 工程 | 开发集 | 留出集 |
| --- | --- | --- |
| Stockroom 仓库 | `count-intervening-moves`、`movement-export-resume`、`receipt-source-diagnostics`、`transfer-request-replay` | `order-case-packs` |
| Harbor Relay 事件日志 | `archive-incremental-reader`、`compacted-journal-floor`、`watch-scanned-offset`、`worker-claim-fence` | `hierarchical-fanout-index` |
| Crewcall 排班 | `payroll-civil-day-split`、`recurring-shift-clock-change`、`roster-overlap-window` | `calendar-publication-revisions`、`roster-substitution-batch` |
| Quayside 账本 | `bank-remittance-replay`、`invoice-reference-canonical-keys`、`statement-line-fx-rounding` | `invoice-credit-line-allocation`、`payment-archive-currency-cutover` |

公开命令 `python -B verify.py` 先跑既有回归，再复现一条工单场景。隐藏验收在独立
工作区中检查 `verify.py`、文档和既有测试的 SHA-256，重跑公开命令，再校验跨层、
重启、回滚或边界场景。Stockroom 原版 SQLite schema 也有字节校验。多线程用例使用
固定时钟和同步点。查询和路由的大输入检查分别按 SQLite 指令数和进程 CPU 时间计量。
隐藏脚本最多运行 24 秒。Crewcall 的时区数据来自 IANA tzdb 2025b，已随仓库提供。

## 本地自检

从项目根目录运行。参考解位于 Git 忽略的 `.data/evolve-expert-solutions/<case-id>/`。
其中两个 `shallow-*/` 子目录各存一份可独立运行的浅层修复仓库。自检将原始仓库、
参考解和浅层修复分别复制到临时目录，编译 Python 源文件，再注入隐藏验收脚本。
测试产生的文件不会写入套件。

```bash
export PYTHONPATH="$PWD/src"
V=/home/ubuntu/projects/SuperCLoudAgent/.venv/bin
timeout 240 "$V/python" -B evals/opencode-evolve-expert/check_cases.py .data/evolve-expert-solutions
```

本地自检覆盖 20 个起始仓库、20 个参考解和 40 个浅层修复。起始仓库通过既有回归
后在公开复现处失败，参考解通过隐藏验收。每个浅层修复通过既有回归和公开复现，
随后被独立的隐藏检查拒绝。参考解和浅层修复均留在原工作树，需在该工作树执行自检
或另行取得这些本地目录。

## 平台运行

使用属于 `eval` 租户且有提交权限的令牌。以下请求固定 OpenCode 版本，对开发集
跑一次基线。

```bash
curl -fsS -X POST "${SCA_API_URL}/v1/evals/runs" \
  -H "Authorization: Bearer ${SCA_EVAL_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "suite": "opencode-evolve-expert",
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

用返回的 ID 查询 `GET /v1/evals/runs/<run_id>`，记录逐题隐藏验收结果、轮次、
成本和套件哈希。比较时固定版本、模型、预算和套件哈希。候选确定后用相同参数将
`split` 改为 `holdout`，单独验证留出集。本套件沿用现有评测加载器和 API，
部署时把目录放到 `SCA_EVAL_SUITES_DIR` 下即可。

## 现场校准（2026-11-20）

OpenCode 2.3.0-sca.8 + deepseek-flash，开发集 14 题全部通过隐藏验收，每题 1 轮，合计 $0.337
（运行 `0a25d2f0`）。离线自检确认起始仓库和 40 个浅层修复都会被隐藏验收拒绝，因此结果说明该模型
能读懂仓库内的规格，而非验收失效。30–60% 的目标未达到；要拉开差距需改为多步、长程的功能开发题，
或换用更弱的模型做基线。
