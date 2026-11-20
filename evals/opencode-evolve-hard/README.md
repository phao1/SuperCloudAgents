# OpenCode 演进基准：困难层

24 个离线、确定性的 Python 3.12 编码任务，16 个 dev、8 个 holdout。
8 个类别每类恰好 2 个 dev、1 个 holdout。每题仓库与验收只使用标准库，
镜像为 `python:3.12`；一次隐藏验收的本地超时为 19 秒。
套件设置 `max_fix_attempts: 1`、`turn_timeout_s: 420`、`memory_enabled: false`。

目标是让相同 OpenCode + deepseek-flash 基线的首次通过率约为 30–60%，
从而观察系统提示词、搜索与工具指引的收益。**这个区间是目标，不是实测结果。**
本套件只做了本地参考解验证，尚未运行真实模型或 Docker。不能用参考解的通过率
代替模型通过率；负责人应固定 OpenCode 版本、模型、overlay、预算和套件哈希，
先测 dev，再做独立 holdout 门禁。不要根据 holdout 失败修改候选提示词。

## 类别与难点

| 类别 | dev 用例 | holdout 用例 | 行为难点 |
| --- | --- | --- | --- |
| parser | ini-continuations、csv-record-machine | cron-expansion | 状态优先级、严格错误语法、物理行号、完整输入校验；宽松标准库默认行为不足以满足契约 |
| state-machine | ledger-idempotency、lease-expiry | job-transition-replay | 幂等回放、冲突、事务回滚、嵌套字典身份、过期边界、重复动作与合法重试的区别 |
| streaming | stream-group-runs、stream-merge-ties | stream-window-stride | 构造时惰性、精确预读上限、key 调用次数、无限输入、异常对象身份；有限列表上的正确输出不足以通过 |
| unicode | terminal-cell-width、unicode-span-patch | normalized-index-map | 明确的码点宽度策略、组合符与制表符、稳定插入顺序、两次 NFC、原始位置与拼写保留 |
| multi-file | duration-policy-migration、redaction-policy-chain | report-dialect-migration | 实现、调用方、运行时配置、文档、CLI 或字节编码及新增测试必须一致；配置不能在导入时缓存 |
| search | search-runtime-roles、search-layer-tombstones | search-longest-suffix | 从入口沿真实导入链定位；每题 21 个仓库文件，包含 12 个弃用模块，其中一个超过 1400 行；修错模块无效 |
| large-file | registry-byte-retries、registry-byte-label | registry-byte-timeout | 每个活动注册表超过 1800 行，另有弃用快照；只能改目标字段，必须保留 BOM、CRLF、分解重音及所有其他字节 |
| iteration | iteration-interval-subtract、iteration-semver-sort | iteration-unified-hunks | 现有不可修改的 26、27、24 项 unittest 部分失败；必须修实现并跑完整测试，公开 smoke 通过并不代表完成 |

各题额外关注点：

- `ini-continuations`：缩进续行优先于节标题，注释不清除续行目标，报错使用物理行号。
- `csv-record-machine`：引号结束后的空格也非法，嵌入 CRLF 原样保留，空记录与空文件不同。
- `cron-expansion`：单数值带步长延伸到上界；即使 `*` 已覆盖全部仍要验证后续项。
- `ledger-idempotency`：相同事件只扣一次；后续错误不能留下新增键或改变原字典身份。
- `lease-expiry`：空字符串 owner 合法；无效 TTL 不得调用时钟；恰好到期就不可续租。
- `job-transition-replay`：首次 retry 非法，但重试后的重复 retry 合法；重启只在规定状态允许。
- `stream-group-runs`：一个预读元素的 key 只能计算一次；不可用哈希替代相等比较。
- `stream-merge-ties`：左侧优先的稳定合并；yield 后不能提前补充刚消耗的源。
- `stream-window-stride`：跨窗口跳过也不能提前发生；步长大于宽度时内存仍只随宽度增长。
- `terminal-cell-width`：规则是题目明确给出的码点策略；不能用通用字素宽度库替代。
- `unicode-span-patch`：同位置插入先于替换，保留输入顺序；内部插入属于重叠。
- `normalized-index-map`：归一化分组不等于原始拼写去重；跳过空词仍计入原始位置。
- `duration-policy-migration`：默认值、最大值、单位转换、错误出口与文档必须同步。
- `redaction-policy-chain`：敏感键值即使为空也要脱敏；返回值不能与输入共享可变后代。
- `report-dialect-migration`：web 带 BOM、CLI 不带 BOM；分号、CRLF、引号转义必须一致。
- `search-runtime-roles`：权限按首次出现排序，目录替换和别名冲突不能被硬编码掩盖。
- `search-layer-tombstones`：按键存在覆盖，删除后再引入改变顺序；嵌套值替换而非递归合并。
- `search-longest-suffix`：最长后缀优先，双平台路径分隔符，点文件和尾随空格均有明确语义。
- 三个 `registry-byte-*`：分别改数值零、中文 emoji 标签、超时零；对非目标区间分别做 SHA-256 校验，目标行也逐字节校验。
- `iteration-interval-subtract`：先分别求并集再求差集；空集合不能跳过另一侧的非法区间。
- `iteration-semver-sort`：数值预发布标识、ASCII 标识和忽略构建信息后的稳定顺序各有不同规则。
- `iteration-unified-hunks`：零长度区间坐标不同，第二个 hunk 的新坐标受前一个修改影响。

## 公开验证与隐藏验收

每题包含 `task.yaml`、`repo/verify.py`、`acceptance.yaml` 和 `acceptance/hidden.py`。
需求写全隐藏检查涉及的规则；公开验证只有 1–2 个正常路径断言。
隐藏验收首先核对受保护文件的 SHA-256，然后重新运行 `verify.py`，再验证边界条件。
搜索题保护目录和弃用文件，迭代题保护完整现有测试，大文件题另外检查活动文件的非目标区域。
跨文件题还要求新增至少三个通过的 unittest 方法，并校验配置、文档和真实调用链。
运行时配置替换、源迭代器、注入时钟均为本地确定性夹具，不访问服务、网络或真实时间。

隐藏验收与参考解不进入 Agent 的起始仓库。参考解完整仓库仅放在被忽略的
`.data/evolve-hard-solutions/<case-id>/`，**不能放进 evals 或套件归档**。
不修改 `evals/opencode-evolve/`；该目录可能正在被真实演进运行固定哈希。

## 本地自检

从项目根目录运行，不需要 Docker、Postgres、模型或联网：

```bash
timeout 180 .venv/bin/python evals/opencode-evolve-hard/check_cases.py .data/evolve-hard-solutions
```

`check_cases.py` 是旧套件自检脚本的独立副本，入口固定为当前目录的 `cases/`，
每次把仓库复制到临时目录、编译全部 Python 文件，再注入隐藏验收执行。
每题要求未修改仓库验收失败、完整参考仓库验收通过；源码语法错误不能充当起始失败。
单次验收超时 19 秒；超时被报告为自检失败，而不会被算作预期的起始失败。
本地结果：**24 passed, 0 failed (24 cases)**，即 24 次起始失败、24 次参考解成功。
额外探针中，未修改仓库有 21/24 通过公开 smoke，但全部未通过隐藏验收；
在参考仓库篡改 `verify.py` 后，24/24 被拒绝。24 个参考解的单次隐藏验收
最慢为本机实测 0.716 秒；该时间仅描述本次本地环境。

套件形状和平台加载验证：

```bash
timeout 900 .venv/bin/pytest tests/unit/test_eval_splits.py tests/unit/test_evolve_hard_suite.py -q -p no:cacheprovider
```

若系统的默认 pytest 临时目录属于其他用户，可追加独立的
`--basetemp=/tmp/sca-hard-targeted`。不要在原始 `repo/` 下运行导入或测试，
避免生成 `__pycache__` 等文件改变套件哈希；自检脚本会在临时副本中运行。
平台用 `sca.eval.suites.load_suite` 加载，按 `split` 选择 dev 或 holdout，
哈希始终覆盖整个套件。参考解目录不会进入哈希。
