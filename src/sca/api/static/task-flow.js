/* Project the task's event log onto the original SVG. No timer invents a transition. */
const SCAJobFlow = (() => {
  const terminal = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

  function trace(task, events) {
    const steps = [];
    const tracks = new Map([['main', '主任务']]);
    const turns = new Map();
    const outcomes = new Map();
    const restored = new Set();
    let planned = new Set();
    let planning = false;
    let planReview = false;
    let planAttempt = 0;

    for (const event of events) {
      const p = event.payload || {};
      const kind = event.type;
      const child = typeof p.subtask === 'string' ? p.subtask : '';
      const planEvent = kind.startsWith('plan.') || (planning && child === 'plan-' + planAttempt);
      const scope = planEvent || (kind === 'review.requested' && p.plan)
        || (kind === 'review.submitted' && planReview)
        ? 'plan' : child ? 'child:' + child : 'main';

      const previousTurn = turns.get(scope) || 0;
      const turn = scope === 'plan' ? 0 : Number.isInteger(p.turn) ? p.turn : previousTurn;
      turns.set(scope, Math.max(previousTurn, turn));
      const add = (label, paths, state = 'seen', focus) => {
        if (scope === 'plan') tracks.set('plan', '任务规划');
        if (scope.startsWith('child:')) tracks.set(scope, '子任务 · ' + child);
        const nodes = new Set(), edges = new Set();
        for (const path of paths) {
          const chain = path.split('>');
          chain.forEach(id => nodes.add(id));
          for (let i = 1; i < chain.length; i++) edges.add(chain[i - 1] + '>' + chain[i]);
        }
        steps.push({ seq: event.seq, time: event.created_at, scope, turn, label, state,
          focus: focus || [...nodes].at(-1), nodes: [...nodes], edges: [...edges],
          context: tracks.get(scope) + (turn ? ' · 第 ' + turn + ' 轮' : '') });
      };

      if (kind === 'task.created') {
        const paths = ['ui_goal>ui_dispatch>task_entry'];
        if (task.result?.trace_id) paths.push('obs_trace');
        if (task.spec?.eval_run_id) paths.push('evo_goal>evo_eval');
        if (task.spec?.labels?.kind === 'opencode-fix') paths.push('evo_eval>evo_improve');
        add('任务下发并接入', paths, 'seen', 'task_entry');
      } else if (kind === 'admission.waiting' || kind === 'admission.granted') {
        const waiting = kind.endsWith('waiting');
        const position = p.position ?? task.result?.admission?.position;
        add(waiting ? '等待执行槽位' + (position ? ' · 第 ' + position + ' 位' : '') : '获得执行槽位',
          ['task_entry'], waiting ? 'waiting' : 'seen');
      } else if (kind === 'plan.started') {
        planning = true;
        planReview = false;
        planAttempt = p.attempt || 1;
        const path = planAttempt > 1 ? 'ui_confirm>optim_entry>planner' : 'task_entry>planner';
        add('规划尝试 ' + planAttempt + ' · 理解任务', [path]);
      } else if (kind === 'plan.completed') {
        planning = false;
        planned = new Set((p.subtasks || []).map(item => item.name));
        planReview = Boolean(p.split && task.spec?.plan_review);
        add(p.split ? '解耦完成 · ' + planned.size + ' 个子任务' : '规划完成 · 单任务执行',
          [p.split ? 'planner>decouple' : 'planner']);
      } else if (kind === 'plan.rejected') {
        planning = false;
        planned.clear();
        add('规划未通过 · 回退到单任务', ['planner'], 'failed');
      } else if (kind === 'intents.compiled') {
        add('设计或工单约束已注入',
          ['ui_dispatch>design_in>design_spec>design_intent>task_entry'], 'seen', 'design_intent');
      } else if (kind === 'memory.injected') {
        const count = p.ids?.length || 0;
        add(count ? '注入项目记忆 · ' + count + ' 条' : '已查询项目记忆', ['memory']);
      } else if (kind === 'subtask.started') {
        add('进入子任务队列 · ' + child,
          planned.has(child) ? ['decouple>dag>subtask_q'] : ['dag>subtask_q']);
      } else if (kind === 'workspace.prepared' || kind === 'workspace.forked') {
        const paths = ['dag'];
        if (p.method === 'overlay' || p.workspace_method === 'overlay' || p.cache_hit || p.snapshot_id)
          paths.push('cache_code');
        add(kind.endsWith('forked') ? '从准备快照恢复工作区' : '工作区就绪', paths, 'seen', 'dag');
      } else if (kind === 'sandbox.released') {
        add('释放执行沙箱，保留工作区', ['sandbox_pool>recover_gate']);
      } else if (kind === 'sandbox.created' || kind === 'sandbox.restored') {
        if (kind.endsWith('restored')) {
          restored.add(scope);
          add('重新接入沙箱与会话', ['sandbox_pool>recover_gate', 'compute'], 'seen', 'recover_gate');
        } else if (planEvent) {
          add('建立规划沙箱', ['planner', 'sandbox_sched>sandbox_pool', 'compute'], 'seen', 'sandbox_pool');
        } else if (child) {
          const path = planned.has(child) ? 'decouple>dag>subtask_q>sandbox_sched>sandbox_pool'
            : 'dag>subtask_q>sandbox_sched>sandbox_pool';
          add('子任务已分配沙箱 · ' + child, [path, 'compute'], 'seen', 'sandbox_pool');
        } else {
          add('分配执行沙箱', ['dag>sandbox_sched>sandbox_pool', 'compute'], 'seen', 'sandbox_pool');
        }
      } else if (kind === 'setup.completed') {
        add(p.skipped ? '无需环境准备' : '环境准备' + (p.exit_code ? '失败' : '完成'),
          p.snapshot_id ? ['sandbox_pool>cache_gw', 'cache_dep'] : ['sandbox_pool'],
          p.exit_code ? 'failed' : 'seen', 'sandbox_pool');
      } else if (kind === 'agent.turn_started') {
        const revision = turn > 1 && turn > previousTurn;
        const reason = outcomes.get(scope);
        outcomes.delete(scope);
        const resumed = restored.delete(scope);
        const label = planEvent ? '规划 Agent 开始执行'
          : revision ? (reason === 'review' ? '根据人工反馈开始' : reason === 'failed' ? '根据验证失败开始'
            : '进入') + '第 ' + turn + ' 轮'
          : '开始第 ' + turn + ' 轮执行';
        const execution = resumed ? 'recover_gate>runtime' : 'sandbox_pool>runtime';
        const retry = reason === 'review' ? 'ui_confirm>optim_entry>runtime'
          : reason === 'failed' ? 'verify_gw>optim_entry>runtime' : 'optim_entry>runtime';
        add(label, revision ? (resumed ? [retry, execution] : [retry]) : [execution], 'seen', 'runtime');
      } else if (kind === 'model.key_issued') {
        add('已为本轮签发模型访问权限', ['identity']);
      } else if (kind === 'agent.command' || kind === 'agent.tool' || kind === 'agent.file_change') {
        const name = kind === 'agent.file_change' ? '编辑文件'
          : kind === 'agent.command' ? '执行命令' : '调用工具' + (p.name ? ' · ' + p.name : '');
        const paths = ['runtime>executor'];
        if (task.spec?.agent_backend === 'opencode-brain') paths.push('executor>hand_brain');
        add(name, paths, p.exit_code ? 'failed' : 'seen');
      } else if (kind === 'agent.usage') {
        add('记录模型用量', ['runtime', 'obs_cost'], 'seen', 'runtime');
      } else if (kind === 'sandbox.activity' && p.segment) {
        add('保存沙箱运行统计', ['obs_store']);
      } else if (kind === 'agent.error') {
        add('Agent 执行错误', ['runtime'], 'failed');
      } else if (kind === 'agent.turn_completed') {
        add(planEvent ? '规划 Agent 已返回' : '本轮 Agent 执行完成', ['runtime']);
      } else if (kind === 'workspace.checkpointed') {
        add('保存本轮提交与检查点', ['runtime>executor>checkpoint']);
      } else if (kind === 'subtasks.merged') {
        const unresolved = (p.results || p.subtasks || []).some(result =>
          !['merged', 'unchanged'].includes(result.status));
        add(unresolved ? '已装配子任务 · 有未合并结果' : '装配并合并子任务',
          ['executor>dag'], unresolved ? 'failed' : 'seen');
      } else if (kind === 'verification.started' || kind === 'ci.requested') {
        const waitForCi = kind === 'ci.requested';
        add(waitForCi ? '分支已推送 · 等待 CI' : '开始执行验证',
          waitForCi ? ['executor>repos', 'sandbox_pool>verify_gw'] : ['sandbox_pool>verify_gw'], 'waiting');
      } else if (['verification.completed', 'ci.completed', 'intents.checked'].includes(kind)) {
        const passed = kind === 'intents.checked' ? (p.checks || []).every(check => check.passed) : p.passed;
        if (passed === false || !outcomes.has(scope)) {
          outcomes.set(scope, passed === false ? 'failed' : 'passed');
        }
        const label = p.skipped ? '未配置验证命令 · 已跳过'
          : (kind === 'ci.completed' ? 'CI' : kind === 'intents.checked' ? '意图检查' : '验证')
            + (passed ? '通过' : '未通过');
        const paths = [p.skipped ? 'verify_gw' : 'sandbox_pool>verify_gw>verify_parser>quality_echo>ui_quality'];
        if (steps.some(step => step.scope === scope && step.turn === turn && step.focus === 'checkpoint'))
          paths.push('checkpoint>sandbox_pool');
        add(label, paths, passed === false ? 'failed' : 'seen', p.skipped ? 'verify_gw' : 'ui_quality');
      } else if (kind === 'acceptance.completed') {
        add(p.passed ? '独立验收通过' : '独立验收未通过',
          ['verify_parser>quality_echo>ui_quality', 'evo_eval'], p.passed ? 'seen' : 'failed', 'evo_eval');
      } else if (kind === 'artifact.published') {
        add(p.pushed ? '补丁已发布 · 分支已推送' : '补丁已发布',
          p.pushed ? ['executor>repos', 'object_store'] : ['object_store'], 'seen', p.pushed ? 'repos' : 'object_store');
      } else if (kind === 'review.requested') {
        if (!task.spec?.auto_finish || p.plan)
          add(p.plan ? '等待审核拆分方案' : '等待人工审核补丁',
            [p.plan ? 'planner>ui_approve' : 'ui_quality>ui_confirm'], 'waiting');
      } else if (kind === 'review.submitted') {
        const reviewNode = planReview ? 'ui_approve' : 'ui_confirm';
        const audit = p.actor ? ['audit'] : [];
        if (p.decision === 'request_changes') {
          if (!planReview) outcomes.set('main', 'review');
          add(planReview ? '要求重新规划' : '收到修改意见 · 等待下一轮',
            [planReview ? reviewNode : 'ui_confirm>optim_entry', ...audit], 'waiting',
            planReview ? reviewNode : 'optim_entry');
        } else {
          add(p.decision === 'approve' ? '人工审核通过' : '人工驳回', [reviewNode, ...audit],
            p.decision === 'approve' ? 'seen' : 'failed', reviewNode);
        }
        planReview = false;
      } else if (kind === 'task.state_changed') {
        if (p.to === 'PROVISIONING')
          add('工作流开始准备', [task.spec?.plan==='auto'?'task_entry':'task_entry>dag']);
        if (p.to === 'VERIFYING' && !planReview && !planning)
          add('进入验证环节', ['sandbox_pool>verify_gw'], 'waiting', 'verify_gw');
        if (p.to === 'AWAITING_REVIEW' && !planReview && !task.spec?.auto_finish
          && steps.at(-1)?.focus !== 'ui_confirm')
          add('等待人工审核补丁', ['ui_quality>ui_confirm'], 'waiting');
        if (p.to === 'PAUSED') add('任务已暂停', ['recover_gate'], 'waiting');
        if (terminal.has(p.to)) {
          const lastFocus = steps.at(-1)?.focus || 'task_entry';
          if (p.to === 'SUCCEEDED') {
            const reviewed = !task.spec?.auto_finish && steps.some(step =>
              step.scope === 'main' && step.focus === 'ui_confirm');
            add('任务完成 · 查看历史', reviewed ? ['ui_confirm>ui_history'] : ['ui_history']);
          } else {
            add(p.to === 'CANCELLED' ? '任务已取消' : '任务失败', [lastFocus], 'failed');
          }
        }
      }
    }
    return { steps, tracks };
  }

  function project(steps, cursor) {
    const nodes = new Map(), edges = new Map();
    const current = steps[cursor];
    for (const step of steps.slice(0, cursor + 1)) {
      for (const id of step.nodes) {
        const history = nodes.get(id)?.events || [];
        history.push({ seq: step.seq, label: step.label, context: step.context });
        if (history.length > 8) history.shift();
        const state = step.focus === id && step.state === 'failed' ? 'failed'
          : step === current && step.focus === id ? 'current' : 'seen';
        nodes.set(id, { id, state, events: history });
      }
      for (const id of step.edges)
        edges.set(id, { id, state: step === current ? 'current' : 'seen' });
    }
    return { nodes: [...nodes.values()], edges: [...edges.values()] };
  }

  function excerpt(value, limit = 240) {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    return text.length > limit ? text.slice(0, limit) + '…' : text;
  }

  function workspacePath(value) {
    const path = excerpt(value);
    if (path === '/workspace' || path === '/workspace/') return '仓库根目录';
    return path.replace(/^\/workspace\//, '');
  }

  function instructionExcerpt(value, limit) {
    return excerpt(typeof value === 'string' ? value.split(/^[ \t]*(?:`{3,}|~{3,})/m, 1)[0] : '', limit);
  }

  function readTarget(event) {
    const p = event.payload || {};
    return workspacePath(p.input?.filePath || p.input?.path || p.filePath || p.path);
  }

  function workflowPlan(history, event, future = false) {
    return history.findLast(item => item.type === 'plan.completed' &&
      (future ? item.seq > event.seq && (item.payload?.attempt || 1) === (event.payload?.attempt || 1)
        : item.seq <= event.seq));
  }

  function qualityReport(task, events) {
    const latest = new Map(), ci = [];
    const kinds = {
      'verification.completed': '工作区验证', 'intents.checked': '设计规则检查',
      'acceptance.completed': '独立验收', 'ci.completed': 'CI',
    };
    for (const event of events) {
      const label = kinds[event.type];
      if (!label) continue;
      const p = event.payload || {};
      const checks = Array.isArray(p.checks) ? p.checks : [];
      const passed = event.type === 'intents.checked' && checks.length
        ? checks.every(check => check.passed === true) : p.passed;
      const row = { seq: event.seq, type: event.type, label, scope: p.subtask || '主任务',
        turn: p.turn, passed, skipped: p.skipped, commit: p.commit, checks };
      if (event.type === 'ci.completed') ci.push(row);
      else latest.set(event.type + ':' + row.scope, row);
    }
    return { rows: [...latest.values(), ...ci].sort((a, b) => a.seq - b.seq),
      verification: task.result?.verification_passed, acceptance: task.result?.acceptance_passed,
      evaluation: task.spec?.eval_run_id };
  }

  function describeStep(task, event, history = [], config = {}) {
    const p = event?.payload || {};
    const spec = task.spec || {};
    const facts = [], items = [];
    let summary = '', note = '', delay = 1300, quality = false;
    const fact = (label, value) => {
      if (value != null && value !== '') facts.push({ label, value: String(value) });
    };
    const item = (title, text, detail) => items.push({ title, text, detail });
    const result = value => value === true ? '通过' : value === false ? '未通过' : '';
    const prior = matches => history.findLast(e => e.seq < event.seq && matches(e));
    const branch = () => {
      fact('分支', p.branch);
      fact('提交', excerpt(p.commit || p.base_sha, 12));
    };
    const routing = () => {
      fact('Temporal 队列', p.task_queue);
      fact('工作流 ID', p.workflow_id);
      fact('执行 Worker', p.worker);
    };
    const parts = workflowPlan(history, event || { seq: 0 })?.payload?.subtasks || spec.subtasks || [];
    const scopedPart = parts.find(child => child.name === p.subtask);
    const taskGoal = () => fact('实际需求', instructionExcerpt(spec.instruction || spec.question, 270));

    switch (event?.type) {
      case 'task.created':
        summary = 'API 接收任务、核验调用权限，并创建一条可追踪的工作流记录。';
        fact('任务类型', { coding: '代码任务', analysis: '分析任务' }[task.kind] || task.kind);
        fact('规划方式', spec.plan === 'auto' ? '自动规划' : spec.subtasks?.length ? '已提供子任务' : '单任务执行');
        taskGoal();
        break;
      case 'admission.waiting':
      case 'admission.granted':
        summary = event.type === 'admission.waiting'
          ? '任务进入租户队列，等待可用的执行槽位。' : '调度器为这个工作流分配了执行槽位，可以继续准备或恢复执行。';
        fact('队列位置', p.position);
        if (typeof p.wait_seconds === 'number') fact('等待', p.wait_seconds.toFixed(2) + ' 秒');
        break;
      case 'plan.started': {
        summary = '规划 Agent 对照需求与仓库内的实现和测试，先划定修改边界，再把方案写入 plan.json。';
        fact('规划尝试', p.attempt);
        taskGoal();
        const completed = workflowPlan(history, event, true);
        if (completed) {
          const planned = completed.payload.subtasks || [];
          fact('本次后续方案', planned.length
            ? planned.map(child => child.name + (child.paths?.length ? '（' + child.paths.join('、') + '）' : '')).join('，')
            : '主任务直接执行');
          fact('方案证据', '事件 #' + completed.seq);
        }
        routing();
        delay = 4600;
        break;
      }
      case 'plan.completed': {
        const children = p.subtasks || [];
        summary = p.split ? '规划器划分了各子任务的工作范围，并检查当前仓库中的文件匹配范围互不重叠。'
          : '规划器选择由主任务在同一个工作区完成。';
        if (p.split) {
          fact('执行关系', '并行执行，各自使用独立工作区和沙箱。全部返回后由主任务装配并验证。');
          if (spec.plan_review) fact('开始条件', '拆分方案获批后下发');
          for (const child of children) item(child.name, instructionExcerpt(child.instruction, 220), child.paths?.join('、'));
        }
        const inspected = [...new Set(history.filter(entry => entry.seq < event.seq && entry.type === 'agent.tool'
          && entry.payload?.subtask === 'plan-' + (p.attempt || 1) && entry.payload?.name === 'read')
          .map(readTarget).filter(Boolean))];
        if (inspected.length) fact('规划读取过', inspected.slice(0, 8).join('、'));
        note = excerpt(p.reason);
        delay = p.split ? 4000 + Math.min(children.length, 5) * 900 : 3300;
        break;
      }
      case 'plan.rejected':
        summary = '规划结果未通过校验，工作流改为单任务执行。';
        note = excerpt(p.reason);
        break;
      case 'subtask.started': {
        summary = 'Temporal 将子工作流加入任务队列。空闲 Worker 领取准备活动，从同一基准提交检出子分支，然后申请独立沙箱。';
        const index = parts.indexOf(scopedPart);
        fact('业务分工', index >= 0 ? '第 ' + (index + 1) + '/' + parts.length + ' 个分支 · ' + p.subtask : p.subtask);
        fact('同批并行', parts.filter(candidate => candidate.name !== p.subtask).map(candidate => candidate.name).join('、'));
        if (scopedPart) item(scopedPart.name, instructionExcerpt(scopedPart.instruction, 260), scopedPart.paths?.join('、'));
        routing();
        if (!p.worker) note = '这条历史事件保留了业务分支。执行它的 Worker 标识从新版事件开始记录。';
        delay = 4150;
        break;
      }
      case 'subtasks.merged': {
        const statuses = p.subtasks || p.results || [];
        summary = '并行分支都返回后，装配器尝试把各自的提交合并到主工作区，再进行全量验证。';
        for (const child of statuses) {
          const status = { merged: '已合并', unchanged: '无改动', conflict: '有合并冲突' }[child.status];
          const worker = prior(e => e.type === 'agent.turn_started' && e.payload?.subtask === child.name)?.payload?.worker;
          const change = prior(e => e.type === 'workspace.checkpointed' && e.payload?.subtask === child.name);
          const purpose = parts.find(part => part.name === child.name);
          const files = (Array.isArray(change?.payload?.changed) ? change.payload.changed : purpose?.paths || [])
            .map(workspacePath).filter(Boolean);
          item(child.name, [status || child.status, result(child.passed), worker && 'Worker ' + worker].filter(Boolean).join(' · '),
            [files.join('、'), excerpt(child.reason), child.branch].filter(Boolean).join(' · '));
        }
        delay = 4000;
        break;
      }
      case 'workspace.prepared':
      case 'workspace.forked':
        summary = event.type === 'workspace.forked' ? '从准备好的快照建立本次任务的工作区。'
          : p.mirror_only ? '准备 Git 仓库镜像和基准提交，供规划器和后续子任务建立工作区。'
          : task.kind === 'analysis' ? '把输入数据装入分析工作区，并将输入文件设为只读。'
          : '检出任务要使用的代码版本，并准备隔离的工作分支。';
        branch();
        fact('快照', p.snapshot_id);
        break;
      case 'sandbox.created':
      case 'sandbox.restored': {
        const restored = event.type === 'sandbox.restored';
        const earlier = restored && prior(e => e.type === 'sandbox.created' && e.payload?.sandbox_id === p.sandbox_id)?.payload;
        const driver = p.driver || earlier?.driver;
        const drivers = { docker: 'Docker 容器', k8s: 'Kubernetes Pod' };
        const provision = { docker: '通过 Docker 启动或复用容器', k8s: '创建或领取 Kubernetes Pod，并等待就绪' };
        summary = restored ? '恢复执行时，Worker 重新分配沙箱、接回保存的工作区，并重跑初始化命令。任务和已保存的会话仍可续接。'
          : driver ? 'Worker ' + (provision[driver] || '准备隔离的沙箱') + '，按资源限额和镜像配置环境，再交给 Agent 或验证命令。'
          : '调度器为该执行分支分配独立沙箱，并给 Agent 或验证命令准备工作区。';
        fact(restored ? '恢复对象' : '调度对象', p.subtask || '主任务');
        fact(driver ? '运行方式' : '当前环境', drivers[driver || config.sandbox_driver]);
        fact('镜像', p.image || earlier?.image);
        fact('沙箱名称', p.sandbox_id);
        const released = restored && prior(e => e.type === 'sandbox.released' && e.payload?.sandbox_id === p.sandbox_id)?.payload;
        if (released?.container_id) fact('已释放实例', excerpt(released.container_id, 12));
        fact('当前容器实例', excerpt(p.container_id, 12));
        if (released?.container_id && p.container_id && released.container_id !== p.container_id)
          fact('恢复结果', '新容器接回原任务工作区');
        const transport = p.state_transport || earlier?.state_transport;
        fact(transport ? '工作区交付' : '当前工作区交付',
          { mount: '挂载工作目录', sync: '同步工作目录' }[transport || config.state_transport]);
        if (p.cpus || p.memory_mb) fact('资源限额', [p.cpus && p.cpus + ' vCPU', p.memory_mb && p.memory_mb + ' MiB 内存'].filter(Boolean).join(' · '));
        routing();
        if (!driver && provision[config.sandbox_driver]) note = '当前 Worker ' + provision[config.sandbox_driver] + '。这条历史事件记录了沙箱名称，运行方式以当前配置为参考。';
        delay = 4100;
        break;
      }
      case 'sandbox.released':
        summary = 'Worker 已回收执行沙箱并释放执行槽位。工作区和已保存的会话继续留存，供下一次恢复。';
        fact('沙箱名称', p.sandbox_id);
        fact('已释放容器', excerpt(p.container_id, 12));
        fact('释放时的轮次', p.after_turn);
        routing();
        break;
      case 'setup.completed':
        summary = p.skipped ? '工作区已从准备快照恢复，跳过重复的环境初始化。'
          : '在沙箱里执行项目初始化和意图检查需要的安装步骤。';
        fact('环境准备', p.exit_code == null ? '' : result(p.exit_code === 0));
        fact('初始化命令', excerpt(p.command));
        break;
      case 'memory.injected': {
        const count = p.ids?.length;
        summary = 'Worker 在当前租户查找已启用的记忆，按租户、任务类型和仓库范围匹配。命中的项目约定、常用命令和踩坑经验会写入工作区，作为 Agent 的参考。';
        fact('本次注入', count == null ? '' : count + ' 条');
        fact('查询范围', ['当前租户', task.kind && '类型 ' + task.kind, spec.repo_url && '同一代码仓'].filter(Boolean).join(' · '));
        fact('挑选方式', { all: '范围内全部匹配项', recent: '优先最近使用或创建的条目', vector: '按语义相关性筛选' }[p.method]);
        if (count) {
          fact('写入位置', '.sca/context/MEMORY.md');
          for (const entry of p.entries || []) {
            const category = { command: '常用命令', pitfall: '踩坑经验', convention: '项目约定', preference: '偏好' }[entry.category];
            const scope = { repo: '仓库', kind: '任务类型', tenant: '租户' }[entry.scope];
            item(category || '匹配记忆', excerpt(entry.text, 260), scope && scope + '作用域');
          }
          if (!items.length) note = excerpt(task.result?.memory_context, 520);
        }
        delay = 4900;
        break;
      }
      case 'intents.compiled':
        summary = '读取提交的规范和规则，编译成 Agent 可参考的工作区上下文及可执行的验收检查。';
        for (const source of p.sources || []) item(source.name, source.type, source.checks?.join('、'));
        break;
      case 'agent.turn_started':
        summary = 'Worker 把这一执行分支的需求交给 Agent。重试时会附上上次验证或审核提供的反馈。';
        fact('后端', p.backend || spec.agent_backend);
        fact('模型', spec.model);
        fact('执行轮', p.turn === 0 ? '规划轮' : p.turn);
        if (scopedPart) item(scopedPart.name, instructionExcerpt(scopedPart.instruction, 240), scopedPart.paths?.join('、'));
        else if (p.turn > 1) {
          const feedback = prior(e => !e.payload?.subtask && (e.type === 'ci.completed' && e.payload?.passed === false
            || e.type === 'review.submitted' && e.payload?.decision === 'request_changes'));
          if (feedback) fact('本轮反馈', excerpt(feedback.payload.summary || feedback.payload.feedback, 360));
        }
        routing();
        break;
      case 'model.key_issued':
        summary = '模型网关为本轮签发短期访问凭据，Agent 通过网关请求所选模型。';
        fact('模型', p.model);
        break;
      case 'agent.command':
      case 'agent.tool':
      case 'agent.file_change':
        summary = { 'agent.command': '执行器在沙箱里运行命令，例如读取仓库、运行测试或处理文件。',
          'agent.tool': 'Agent 请求执行器调用这项工具，并拿回执行结果。',
          'agent.file_change': '执行器记录这一轮对工作区文件的修改。' }[event.type];
        fact('工具', p.name);
        fact('命令', excerpt(p.command, 160));
        fact('退出码', p.exit_code);
        if (p.name === 'read') {
          summary = 'Agent 打开文件或目录，核对实现、测试或项目说明。';
          fact('读取对象', readTarget(event));
          const span = /\b(lines|entries)\s+(\d+)-(\d+)/i.exec(p.output_tail || '');
          if (span) fact('实际读取范围', (span[1].toLowerCase() === 'lines' ? '行' : '条目') + ' ' + span[2] + '–' + span[3]);
        } else if (p.name === 'glob') {
          summary = 'Agent 按文件名模式查找候选文件，结果为后续读取、规划或修改提供范围。';
          fact('匹配模式', excerpt(p.input?.pattern));
          fact('搜索目录', readTarget(event));
          const matches = (p.output_tail || '').split('\n').map(line => line.trim())
            .filter(line => line.startsWith('/workspace/')).slice(0, 8).map(workspacePath);
          if (matches.length) fact('结果节选', matches.join('、'));
          else if (/no files found/i.test(p.output_tail || '')) fact('结果', '未找到匹配文件');
        } else {
          const files = (p.changes || []).map(change => workspacePath(typeof change === 'string'
            ? change : change.file || change.path || change.filePath));
          fact('文件', files.filter(Boolean).join('、') || readTarget(event) || workspacePath(p.file));
        }
        if (scopedPart) fact('业务分支', scopedPart.name);
        delay = 1000;
        break;
      case 'agent.usage':
        summary = '记录这次模型调用的 Token 和费用，汇总到任务用量和预算。';
        fact('输入 Token', p.input_tokens);
        fact('输出 Token', p.output_tokens);
        fact('缓存 Token', p.cached_input_tokens);
        delay = 900;
        break;
      case 'agent.error':
        summary = 'Agent 本轮报告执行错误，工作流记录失败原因。';
        note = excerpt(p.message || p.error);
        break;
      case 'agent.turn_completed':
        summary = p.turn === 0 ? '规划 Agent 交回方案，工作流随后检查拆分结构与文件范围。'
          : 'Agent 结束本轮并把结果交回工作流，随后保存检查点并运行验证。';
        break;
      case 'workspace.checkpointed':
        summary = '保存本轮工作区的提交与检查点，供后续验证、发布或恢复执行。';
        branch();
        if (Array.isArray(p.changed)) fact('本轮改动', p.changed.map(workspacePath).filter(Boolean).join('、'));
        break;
      case 'verification.started':
        summary = '验证器开始在沙箱中运行任务配置的检查。有多个检查时会并行执行。';
        fact('检查项', p.checks);
        fact('验证对象', p.subtask || '主任务工作区');
        for (const command of [spec.verify_command, ...(spec.verify_commands || [])].filter(Boolean))
          item('配置的命令', excerpt(command, 430));
        routing();
        break;
      case 'verification.completed':
      case 'intents.checked': {
        const checks = p.checks || [];
        summary = p.skipped ? '本任务未配置验证命令，本步跳过。'
          : event.type === 'intents.checked' ? '执行从设计规范或规则编译出来的检查，并记录逐项结果。'
          : '收集本轮的验证退出码和输出，将失败项交给工作流决定是否重试。';
        fact('检查结果', result(p.passed));
        for (const [index, check] of checks.entries()) item((check.name || '检查 ' + (index + 1)) + ' · '
          + (check.timed_out ? '超时' : result(check.passed)), excerpt(check.command, 430),
          check.exit_code == null ? '' : '退出码 ' + check.exit_code);
        const testCount = /\bRan\s+(\d+)\s+tests?/i.exec(p.output_tail || '');
        if (testCount) fact('测试输出', '运行 ' + testCount[1] + ' 项测试');
        note = p.passed === false ? excerpt(p.output_tail, 300) : '';
        quality = true;
        delay = checks.length > 1 ? 2400 : 1500;
        break;
      }
      case 'ci.requested':
      case 'ci.completed':
        summary = event.type === 'ci.requested' ? '推送本轮分支并等待 CI 对该提交回报结果。'
          : 'CI 把判定回报给工作流，按提交匹配本轮代码。失败的摘要可用于下一轮修改。';
        branch();
        fact('CI 结果', p.timed_out ? '等待超时' : result(p.passed));
        note = excerpt(p.summary, 360);
        quality = event.type === 'ci.completed';
        delay = 3600;
        break;
      case 'acceptance.completed':
        summary = '用本轮提交建立一份独立工作区，运行随任务登记的验收检查。';
        fact('验收结果', result(p.passed));
        fact('退出码', p.exit_code);
        fact('验收命令', excerpt(spec.acceptance_command));
        note = excerpt(p.output_tail, 320);
        quality = true;
        delay = 2300;
        break;
      case 'artifact.published':
        summary = task.kind === 'analysis' ? '从提交中提取分析产物，保存以供审核或下载。'
          : '将最终差异保存为任务产物，供后续审核与下载。';
        fact('产物', p.name);
        if (p.pushed) fact('发布动作', '已推送任务分支');
        branch();
        break;
      case 'review.requested':
      case 'review.submitted':
        summary = event.type === 'review.submitted' ? '工作流记录审核结论，并据此继续、修改或结束。'
          : p.plan ? '暂停下发子任务，等待审核拆分方案。' : '暂停后续交付，等待审核本轮补丁。';
        fact('审核结论', { approve: '批准', request_changes: '要求修改', reject: '驳回' }[p.decision]);
        note = excerpt(p.feedback);
        break;
      case 'sandbox.activity':
        summary = '把沙箱存续时间和实际活跃时间写入任务记录，便于回溯资源占用。';
        if (typeof p.sandbox_alive_s === 'number') fact('存续时间', p.sandbox_alive_s.toFixed(1) + ' 秒');
        if (typeof p.sandbox_active_s === 'number') fact('活跃时间', p.sandbox_active_s.toFixed(1) + ' 秒');
        break;
      case 'task.state_changed':
        summary = { PROVISIONING: '工作流进入准备阶段，检查任务规格并准备规划或工作区。',
          VERIFYING: '本轮交回的工作区进入测试和验收流程。',
          AWAITING_REVIEW: '本轮结果已提交，工作流等待审核决定。',
          PAUSED: '工作流已在执行轮之间暂停，等待继续指令。',
          SUCCEEDED: '工作流已完成，将本次事件和产物留在任务历史里。',
          FAILED: '工作流以失败结束，查看这一步的原始记录可以定位原因。',
          CANCELLED: '工作流已取消，保留取消前的任务事件。' }[p.to] || '';
        note = excerpt(p.reason);
        break;
    }
    return { summary, facts, items, note, delay, quality };
  }

  return { trace, project, describeStep, qualityReport };
})();
if (typeof module === 'object') module.exports = SCAJobFlow;
