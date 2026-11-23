# Phase 6 代码解耦与影响分析

## 提交子任务

调用 `POST /v1/tasks` 时，可以为每个编码子任务指定 `depends_on` 和 `paths`。依赖名必须来自同一任务，且依赖关系必须无环。每个子任务在其依赖通过验证后，从合并后的依赖提交启动。没有依赖的子任务可以并行。依赖失败或合并冲突时，受影响的子任务以 `blocked` 状态进入原有的人工审核流程。

```bash
curl -X POST "$SCA_API/v1/tasks" \
  -H "Authorization: Bearer $SCA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "coding",
    "spec": {
      "repo_url": "https://example.org/team/calc.git",
      "instruction": "为计算模块增加舍入配置并补齐测试",
      "shared_paths": ["src/calc/options.py"],
      "subtasks": [
        {"name": "impl", "instruction": "实现舍入配置", "paths": ["src/calc/**"]},
        {"name": "tests", "instruction": "增加舍入测试", "paths": ["tests/**"],
         "depends_on": ["impl"]}
      ]
    }
  }'
```

`paths` 接受 Git ignore 规则，包含以 `!` 开头的排除规则。任务级 `shared_paths` 填写精确的仓库相对文件路径，允许各子任务修改这些文件。带范围的子任务在每轮检查点比较 Git 树，重命名同时检查旧路径和新路径。越界时丢弃该轮改动，记录含子任务名、轮次和路径的 `scope.violation` 事件，并在 `max_fix_attempts` 额度内用同一会话反馈重试。检查覆盖会进入 Git 补丁的文件改动。

## Portal 自动规划与并发

Portal 的「新建任务」和项目「提交线程」默认使用「自动规划」，可在「任务拆分」选择「无」或「手动子任务」。
规划和子任务沿用表单选择的 Agent 后端与模型。规划器会读到仓库文件、局部导入、符号和候选测试的摘要，
并写出 `.sca/plan.json`。单文件修复等小任务可直接执行。可拆分的需求会形成 2 至 8 个子任务，
用 `depends_on` 指定依赖。能同时运行的子任务须使用互不重叠的写入范围，有依赖关系的子任务可以修改同一文件。
规划会校验已有文件和明确声明的新路径，执行时也会检查每次提交的实际文件改动。规划无效时退回单任务执行。
`"plan_review": true` 会先把方案交给人工确认。规划器可提供 `parallelism` 建议，执行图会展示该建议。

「最多并发 agent 数」留空时使用平台值。本机计算默认值时预留 1536 MiB，按 `SCA_AGENT_MEMORY_MB`
（默认 700）估算每个 Agent 的开销，并受 CPU 数和 1 至 8 的范围约束。可以设置
`SCA_MAX_PARALLEL_AGENTS=auto` 或填写 1 至 8 的整数。单次任务可以用 `max_parallel_agents` 下调上限，
子任务内的 Best-of-N 候选共用该任务的名额。API 提交时可显式指定 `"plan": "auto"`，省略 `plan` 时使用 `"none"`。

```bash
curl -fsS "$SCA_API_URL/v1/portal/config" -H "Authorization: Bearer $SCA_API_TOKEN"

curl -fsS "$SCA_API_URL/v1/tasks" -H "Authorization: Bearer $SCA_API_TOKEN" --json @- <<'JSON'
{"kind":"coding","spec":{"repo_url":"https://example.org/team/app.git",
 "instruction":"定义数据模型，并行实现 API 和页面，再补集成测试",
 "agent_backend":"dsh","model":"deepseek-flash","plan":"auto","max_parallel_agents":2}}
JSON

curl -fsS "$SCA_API_URL/v1/tasks/$TASK_ID/dag" -H "Authorization: Bearer $SCA_API_TOKEN"
```

在执行图中，同一依赖层的子任务并排显示，等待并发名额的节点标记「排队中」。任务对话会显示规划结果、
并发上限、子任务开始和完成时的 Agent 标签。运行期间可对多个子任务[发送插话](operations.md#运行中插话)。

## 使用 Best-of-N

同一个 API 和 Worker 也支持为同一编码需求生成多个候选补丁。通过 `POST /v1/tasks` 提交时，
在编码任务的 `spec` 中设置 `"plan": "none"`、`"subtasks": []` 和 `"best_of": {"n": 2}`。
各尝试从同一份准备快照启动，并在各自的预算内运行。使用 `GET /v1/tasks/{id}/best-of` 查看候选。
需要人工选择时，将 `best_of.select` 设为 `"human"`，再向 `POST /v1/tasks/{id}/best-of/select`
发送 `{"attempt": 2}` 选择第二个候选。完整请求见 [Best-of-N API 示例](design-phase4.md#第六期编码任务-best-of-n)。

## 查询补丁影响

用租户令牌登录 Portal 后，从「任务」打开编码任务。「代码影响分析」会在补丁发布前显示提示，
发布后点击「分析已发布补丁」可查询变更路径、受影响文件和建议测试。

编码任务发布补丁后，使用同一租户的读取权限查询。接口按已发布的提交计算变更，包括 Best-of-N 选中并发布的提交。它合并变更前后索引的反向依赖，返回路径和建议运行的测试。

```bash
curl -H "Authorization: Bearer $SCA_TOKEN" \
  "$SCA_API/v1/tasks/$TASK_ID/impact"
```

```json
{
  "changed_paths": ["src/calc/options.py"],
  "impacted_files": ["src/calc/options.py", "src/calc/rounding.py", "tests/test_rounding.py"],
  "tests_to_run": ["tests/test_rounding.py"],
  "base_sha": "<base commit>",
  "commit": "<published commit>"
}
```

索引存放在 `SCA_DATA_DIR/layers/codegraph-v1`，按租户和 Git 提交复用。Python 使用标准库 AST，支持根目录和 `src/` 布局下的静态导入。JS/TS 使用保守的正则匹配静态相对导入、导出和常见的 `require`。测试映射综合导入关系、测试文件命名和 Python 的 `conftest.py`。动态加载、路径别名和自定义构建配置可能使影响提示不完整，完整验证仍由任务的验证命令负责。本功能使用现有任务事件和 Git 快照，无数据库迁移。

## 验证结果

合并后的单元套件共 691 个通过、13 个跳过。测试使用临时 pytest 适配器，以管道唤醒 asyncio，
并在进程内运行 Moto S3。解耦的 16 个单元用例使用本地 Git 和脚本化 Agent，验证索引、影响分析
API、依赖提交继承、冲突阻塞和两种工作区传输模式下的越界回退。Best-of-N 发布的选中提交也通过了
影响分析 API 检查。

规划、上下文和合并集成用例共 42 个通过。沙箱的套接字限制导致 2 个 Best-of-N Temporal 用例跳过，
32 个工作流用例和 1 个 Postgres 上下文用例在夹具初始化时报错。
