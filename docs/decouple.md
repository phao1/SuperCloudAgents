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

将手动的 `subtasks` 和 `shared_paths` 换成 `"plan": "auto"` 可由规划器生成拆分方案。规划器会收到仓库文件、局部导入、符号和候选测试的摘要。其 `.sca/plan.json` 可填写同样的 `depends_on` 和顶层 `shared_paths`。范围在现有文件上重叠时，需要把重叠文件列入 `shared_paths`。`"plan_review": true` 会先把方案交给人工确认。子任务提示会附上显式共享或被多个子任务使用的 Python 模块的公开函数签名，省略参数默认值和类型注解里的字面量。

## 查询补丁影响

编码任务发布补丁后，使用同一租户的读取权限查询。接口按已发布的提交计算变更，合并变更前后索引的反向依赖，返回路径和建议运行的测试。

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

新增的 16 个单元用例使用本地 Git 和脚本化 Agent，验证索引、影响分析 API、依赖提交继承、冲突阻塞和两种工作区传输模式下的越界回退。工作树中的单元回归共 536 个通过、4 个跳过。沙箱禁止本地套接字，另有 29 个依赖该能力或相应夹具的用例未执行。规划、合并和上下文集成用例共 42 个通过，1 个 Postgres 用例未执行。真实 Postgres、Temporal 和 Docker 端到端流程尚未在该环境验证。
