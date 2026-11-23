'use strict';

const portalConversation = (() => {
  const ui = portalUI;
  const clock = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  const minuteClock = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit'});
  const modes = {queue: '排队', interrupt: '打断', guidance: '指引', next: '下一代生效', now: '立即生效',
    pause: '暂停', resume: '恢复',
    stop: '停止', skip: '跳过', budget: '预算'};
  const pendingLabels = {queue: '排队中 · 当前轮结束后送达', interrupt: '正在打断…',
    next: '排队中 · 等待下一代提议', guidance: '排队中 · 等待下一代提议', now: '正在准备重新提议…'};
  const dollars = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 6});
  const phases = {
    queue: '排队', setup: '准备环境', plan: '规划', agent: '单任务执行', subtasks: '子任务执行',
    verification: '验证', merge: '合并', output: '写入代码', review: '等待审批', paused: '暂停',
    prepare: '准备演进', prepare_evaluation: '准备评测', proposal: '反思与生成候选', gate: '评测门禁',
    evaluation: '评测', evaluation_dev: '开发集评测', evaluation_holdout: '留出集评测',
  };
  const operations = {
    model: '正在调用模型', command: '正在执行命令', tool: '正在调用工具', edit: '正在修改',
    workspace: '正在准备工作区', sandbox: '正在创建沙箱', setup: '正在准备依赖和环境',
    model_retry: '正在等待模型重试', model_rate_limit: '正在等待限流恢复', ci: '正在等待 CI',
    agent: 'Agent 正在执行', slot: '排队等待并发名额', dependency: '正在等待依赖',
    settle: '正在结算费用', checkpoint: '正在保存工作区', verification: '正在验证',
    prepare_evaluation: '正在准备评测', submit_case: '正在提交用例',
  };

  function elapsed(seconds, precise = false) {
    seconds = Math.max(0, seconds);
    if (precise && seconds < 60) return Number(seconds.toFixed(1)) + ' 秒';
    const whole = Math.floor(seconds), minutes = Math.floor(whole / 60), hours = Math.floor(minutes / 60);
    return [hours && hours + ' 小时', minutes % 60 ? minutes % 60 + ' 分' : '',
      whole % 60 || !minutes ? whole % 60 + ' 秒' : ''].filter(Boolean).join(' ');
  }

  function cost(value) {
    return value == null ? '费用待结算' : dollars.format(value);
  }

  function metrics(agent) {
    return [agent.turn === 0 ? '规划轮' : agent.turn != null ? '第 ' + agent.turn + ' 轮' : '',
      (agent.tokens || 0).toLocaleString('zh-CN') + ' tokens', cost(agent.cost_usd)].filter(Boolean).join(' · ');
  }

  function agentName(agent) {
    return [agent.label, ui.backendLabel(agent.backend), agent.subtask].filter(Boolean).join(' · ');
  }

  function operationText(activity) {
    const label = operations[activity.operation] || operations.agent;
    const line = el('span', label);
    if (activity.detail) line.append(' ', el('code', activity.detail));
    return line;
  }

  function summaryCard(snapshot) {
    const card = el('article', undefined, 'conversation-run-summary');
    const heading = el('h3', '运行总结');
    const top = el('div', undefined, 'conversation-summary-metrics');
    top.append(el('strong', '总用时 ' + elapsed(snapshot.elapsed_s, true)),
      el('span', '总费用 ' + cost(snapshot.cost_usd)));
    card.append(heading, top);
    const {phases: stages, agents} = snapshot.summary;
    function rows(title, lines) {
      if (!lines.length) return;
      const details = el('ul');
      details.append(...lines.map(([name, value]) => {
        const row = el('li');
        row.append(el('span', name), el('span', value));
        return row;
      }));
      card.append(el('h4', title), details);
    }
    rows('各阶段用时', stages.map(stage => [phases[stage.phase] || stage.phase, elapsed(stage.duration_s, true)]));
    rows('Agent 用时与费用', agents.map(agent => [agentName(agent),
      [agent.duration_s == null ? '' : elapsed(agent.duration_s, true),
        agent.cost_usd == null ? '费用无记录' : cost(agent.cost_usd)].filter(Boolean).join(' · ')]));
    return card;
  }

  function liveStatus({path, active, isTask, interrupt, logLink, summary, followSummary}) {
    const strip = el('section', undefined, 'conversation-live');
    strip.setAttribute('aria-label', '实时运行状态');
    strip.hidden = true;
    const heading = el('div', undefined, 'conversation-live-heading');
    const status = el('span', undefined, 'badge');
    const total = el('strong'), spend = el('span');
    heading.append(status, total, spend);
    const stage = el('p', undefined, 'conversation-live-phase');
    const detail = el('ul', undefined, 'conversation-live-lanes');
    detail.setAttribute('aria-label', isTask ? 'Agent 活动' : '评测进度');
    const health = el('div', undefined, 'conversation-live-health');
    const fresh = el('strong');
    fresh.setAttribute('role', 'status');
    const recent = el('span');
    const actions = el('div', undefined, 'conversation-live-actions');
    const steer = el('button', isTask ? '打断并发送…' : '立即生效…');
    steer.type = 'button';
    steer.onclick = interrupt.focus;
    const logs = el('span');
    actions.append(steer, logs);
    health.append(fresh, recent, actions);
    const error = el('small', '实时状态暂不可用', 'conversation-live-error');
    error.hidden = true;
    strip.append(heading, stage, health, detail, error);

    let snapshot, received, refreshTimer, ticker, loading, destroyed, signature, logTask;
    let clocks = [], warnings = [];
    const set = (node, text) => { if (node.textContent !== text) node.textContent = text; };
    const age = (at, now) => Math.max(0, (now - Date.parse(at)) / 1000);
    function timer(parent, at, prefix = '') {
      const node = el('span', undefined, 'conversation-live-time');
      parent.append(node);
      clocks.push({node, at, prefix});
    }

    function rebuild() {
      const {agents, evaluation, generation, generation_started_at, candidate, phase, finished} = snapshot;
      const content = JSON.stringify([agents.map(({label, backend, subtask, activity, turn, tokens, cost_usd}) =>
        [label, backend, subtask, activity, turn, tokens, cost_usd]),
      evaluation && {...evaluation, cases: evaluation.cases.map(({task_id, label, state, started_at}) =>
        [task_id, label, state, started_at])}, generation, generation_started_at, candidate, phase, finished]);
      if (signature === content) return;
      signature = content;
      clocks = [];
      warnings = [];
      stage.replaceChildren();
      detail.replaceChildren();
      if (finished) return;
      stage.append(el('span', '当前阶段'), el('strong', phases[phase.operation] || phase.operation));
      timer(stage, phase.started_at, phase.operation === 'queue' ? '已等 ' : '');
      if (phase.detail === 'gateway_pressure') stage.append(el('span', '等待模型网关限流恢复'));
      else if (phase.operation === 'queue' && phase.detail) stage.append(el('span', '等待并发名额'));
      else if (operations[phase.detail]) stage.append(el('span', operations[phase.detail]));
      if (generation != null) {
        const scope = el('li', undefined, 'conversation-live-generation');
        const name = generation === 0 ? '基线' : '第 ' + generation + ' 代';
        scope.append(el('strong', candidate == null ? name : name + ' · 候选 ' + candidate));
        if (generation_started_at) timer(scope, generation_started_at, '本代 ');
        detail.append(scope);
      }
      if (evaluation) addCases(evaluation);
      agents.forEach(addLane);
    }

    function addCases(evaluation) {
      const line = el('li', undefined, 'conversation-live-evaluation');
      const split = {dev: '开发集', holdout: '留出集'}[evaluation.split] || '评测';
      const progress = evaluation.total == null ? evaluation.completed : evaluation.completed + '/' + evaluation.total;
      line.append(el('strong', split), el('span', '已完成 ' + progress + ' 用例 · 通过 ' + evaluation.passed));
      detail.append(line);
      evaluation.cases.forEach((entry, index) => {
        const item = el('li', undefined, 'conversation-live-lane');
        const head = el('div', undefined, 'conversation-live-lane-head');
        head.append(evidenceLink(entry.label, '#/tasks/' + enc(entry.task_id)));
        const line = el('div', undefined, 'conversation-live-operation');
        line.append(el('span', statusText(entry.state)));
        timer(line, entry.started_at);
        const warning = el('small', undefined, 'conversation-live-warning');
        item.append(head, line, warning);
        detail.append(item);
        warnings.push({node: warning, entry: () => snapshot.evaluation.cases[index]});
      });
    }

    function addLane(agent, index) {
      const item = el('li', undefined, 'conversation-live-lane');
      const head = el('div', undefined, 'conversation-live-lane-head');
      head.append(el('strong', agentName(agent)));
      if (agent.turn != null || agent.tokens || agent.cost_usd != null) head.append(el('small', metrics(agent)));
      item.append(head);
      if (agent.activity) {
        const task = el('div', undefined, 'conversation-live-operation');
        task.append(operationText(agent.activity));
        const waiting = ['slot', 'dependency'].includes(agent.activity.operation);
        timer(task, agent.activity.started_at, waiting ? '已等 ' : '');
        item.append(task);
      }
      const warning = el('small', undefined, 'conversation-live-warning');
      item.append(warning);
      detail.append(item);
      warnings.push({node: warning, entry: () => snapshot.agents[index]});
    }

    function tick() {
      if (!active() || destroyed) {
        stop();
        return;
      }
      if (!snapshot) return;
      const now = Date.parse(snapshot.as_of) + performance.now() - received;
      set(total, '总用时 ' + elapsed(snapshot.finished ? snapshot.elapsed_s : age(snapshot.started_at, now)));
      for (const {node, at, prefix} of clocks) set(node, prefix + elapsed(age(at, now)));
      const silence = age(snapshot.last_activity_at, now);
      const stalled = snapshot.running && (snapshot.stalled || silence > snapshot.stall_warn_s);
      const laneStalled = warnings.some(warning => warning.entry().stalled);
      const waiting = snapshot.phase.operation === 'queue';
      const state = snapshot.running ? '运行中' : waiting ? '排队中'
        : snapshot.phase.operation === 'review' ? '等待审批' : statusText(snapshot.state);
      set(status, state);
      detail.tabIndex = detail.scrollHeight > detail.clientHeight ? 0 : -1;
      status.className = 'badge ' + (stalled || laneStalled ? 'waiting'
        : snapshot.running ? 'complete' : waiting ? 'waiting' : statusClass(snapshot.state));
      health.hidden = !snapshot.running;
      health.dataset.tone = stalled || laneStalled ? 'warn' : 'good';
      set(fresh, stalled ? '可能卡住' : laneStalled ? '有步骤可能卡住' : '正常运行');
      set(recent, stalled ? '已 ' + elapsed(silence) + ' 无进展' : '最近活动 ' + elapsed(silence) + '前');
      for (const {node, entry} of warnings) {
        const row = entry();
        node.hidden = !row.stalled;
        if (row.stalled) set(node, '可能卡住 · 已 ' + elapsed(age(row.last_activity_at, now)) + ' 无进展');
      }
      actions.hidden = !stalled && !laneStalled;
      steer.hidden = !interrupt.available();
      const currentLog = snapshot.evaluation?.cases.find(entry => entry.stalled)?.task_id
        ?? snapshot.evaluation?.cases[0]?.task_id;
      if (logTask !== currentLog || !logs.hasChildNodes()) {
        logTask = currentLog;
        const link = logLink?.() || (currentLog && evidenceLink('查看日志', '#/tasks/' + enc(currentLog)));
        logs.replaceChildren(...(link ? [link] : []));
      }
    }

    function stop() {
      clearTimeout(refreshTimer);
      clearInterval(ticker);
      refreshTimer = ticker = undefined;
    }

    async function poll() {
      refreshTimer = undefined;
      if (loading || destroyed || !active() || snapshot?.finished) return;
      loading = true;
      try {
        const next = await data(path + '/live');
        if (!active() || destroyed || !next?.phase) return;
        snapshot = next;
        received = performance.now();
        strip.hidden = false;
        error.hidden = true;
        const totalCost = '费用 ' + cost(next.cost_usd);
        spend.textContent = next.budget_usd == null ? totalCost
          : totalCost + ' · 评测 ' + cost(next.eval_spent_usd) + ' / ' + cost(next.budget_usd);
        rebuild();
        tick();
        if (next.finished) {
          if (next.summary) followSummary(() => summary.replaceChildren(summaryCard(next)));
          stop();
        }
      } catch {
        if (snapshot && active()) error.hidden = false;
      } finally {
        loading = false;
        if (active() && !destroyed && !snapshot?.finished) refreshTimer = setTimeout(poll, 2000);
      }
    }

    return {
      strip,
      start() {
        if (!active() || destroyed || snapshot?.finished) return;
        if (!ticker) ticker = setInterval(tick, 1000);
        if (!refreshTimer && !loading) void poll();
      },
      destroy() { destroyed = true; stop(); },
    };
  }

  function timeLabel(item, format = clock) {
    const time = new Date(item.created_at);
    const timestamp = el('time', Number.isFinite(time.valueOf()) ? format.format(time) : '');
    timestamp.dateTime = item.created_at;
    timestamp.title = ui.date(item.created_at);
    return timestamp;
  }

  function markdown(text) {
    const content = el('div', undefined, 'markdown conversation-markdown');
    content.append(portalMarkdown.render(text));
    if (text.length <= 900 && text.split('\n').length <= 10) return content;
    const details = el('details', undefined, 'conversation-reply');
    const summary = el('summary');
    summary.append(el('span', text.trim().split('\n', 1)[0].slice(0, 200), 'conversation-preview'),
      el('span', '展开', 'conversation-expand'), el('span', '收起', 'conversation-collapse'));
    details.append(summary, content);
    return details;
  }

  function proposalBody(proposal) {
    const body = el('div', undefined, 'conversation-evidence');
    body.append(el('strong', '候选假设'), el('p', proposal.hypothesis));
    if (proposal.rationale) body.append(el('p', '提议依据 · ' + proposal.rationale));
    if (proposal.overlay_changes) {
      const changes = proposal.overlay_changes.join('、');
      body.append(el('small', changes ? '覆盖变更 · ' + changes : '覆盖内容与父候选一致'));
    }
    return body;
  }

  function evaluationBody(evaluation) {
    const title = {baseline: '基线评测', comparison: '当代冠军评测', candidate: '候选评测'};
    const split = {dev: '开发集', holdout: '留出集'}[evaluation.split];
    const body = el('div', undefined, 'conversation-evidence');
    body.append(el('strong', [title[evaluation.role] || '评测结果', split].filter(Boolean).join(' · ')),
      el('p', evaluation.passed + '/' + evaluation.total + ' 通过 · ' + ui.cost(evaluation.cost_usd)));
    if (evaluation.status !== 'completed') body.append(el('small', statusText(evaluation.status)));
    if (evaluation.reason) body.append(el('small', evaluation.reason));
    return body;
  }

  function operationRow(operation) {
    const line = el('li', undefined, 'conversation-operation');
    if (operation.kind === 'command') {
      line.append(el('code', operation.name));
      if (operation.exit_code != null) line.append(el('small', '退出码 ' + operation.exit_code));
      else line.append(el('small', operation.failed ? '执行失败' : '退出码未知'));
      if (operation.output_tail) {
        const output = el('details');
        output.append(el('summary', '输出末尾'), el('pre', operation.output_tail));
        line.append(output);
      }
    } else {
      line.append(el('strong', operation.name));
      if (operation.summary) line.append(el('small', operation.summary));
      if (operation.failed) line.append(el('small', '调用失败', 'bad'));
    }
    if (operation.duration_s != null) line.append(el('small', '用时 ' + elapsed(operation.duration_s, true)));
    line.classList.toggle('conversation-operation-failed', operation.failed);
    return line;
  }

  function operationCard(container, activity) {
    const details = el('details', undefined, 'conversation-activity');
    const heading = el('summary');
    const commands = el('ul', undefined, 'conversation-operation-list');
    const files = el('ul', undefined, 'conversation-file-list');
    const paths = new Map();
    let commandCount = 0, toolCount = 0;
    details.append(heading, commands, files);
    container.append(details);
    function append(chunk) {
      for (const op of chunk.operations) {
        if (op.kind === 'command') commandCount++;
        else toolCount++;
        commands.append(operationRow(op));
      }
      for (const edit of chunk.files) {
        if (!paths.has(edit.path)) {
          const line = el('li');
          line.append(el('code', edit.path));
          const counts = el('small', undefined, 'conversation-edit-counts');
          line.append(counts);
          files.append(line);
          paths.set(edit.path, {counts});
        }
        const entry = paths.get(edit.path);
        for (const field of ['additions', 'deletions']) {
          if (edit[field] != null) entry[field] = (entry[field] ?? 0) + edit[field];
        }
        entry.counts.textContent = [
          entry.additions == null ? '' : '+' + entry.additions,
          entry.deletions == null ? '' : '−' + entry.deletions,
        ].filter(Boolean).join(' / ');
      }
      const summary = '执行了 ' + commandCount + ' 个命令，修改了 ' + paths.size + ' 个文件';
      heading.textContent = toolCount ? summary + ' · ' + toolCount + ' 次工具调用' : summary;
      commands.hidden = commandCount + toolCount === 0;
      files.hidden = paths.size === 0;
    }
    append(activity);
    return {turn: activity.turn_seq, append};
  }

  function create(mount, {path, type, focus, active, canWrite, logLink}) {
    const list = el('ol', undefined, 'conversation-items');
    list.setAttribute('role', 'log');
    list.setAttribute('aria-label', '运行进度与消息');
    list.tabIndex = 0;
    const empty = el('p', '等待运行进度…', 'conversation-empty');
    const loadStatus = el('p', undefined, 'status-text conversation-status');
    loadStatus.setAttribute('role', 'status');
    const feed = el('div', undefined, 'conversation-feed');
    const summary = el('div', undefined, 'conversation-summary');
    feed.append(empty, list, loadStatus, summary);
    const jump = el('button', '有新消息 ↓', 'conversation-jump');
    jump.type = 'button';
    jump.hidden = true;
    const scrollArea = el('div', undefined, 'conversation-scroll-area');
    const approvals = el('div', undefined, 'conversation-approvals');
    scrollArea.append(approvals, feed, jump);

    const isTask = type === 'task';
    const message = isTask ? '插话' : '给提议的指引';
    const shortcuts = isTask ? 'Enter 排队发送 · Ctrl/Cmd+Enter 打断并发送'
      : 'Enter 下一代生效 · Ctrl/Cmd+Enter 立即生效';
    const input = el('textarea', undefined, 'conversation-input');
    Object.assign(input, {name: 'text', required: true, rows: 1, maxLength: 4000, enterKeyHint: 'send',
      placeholder: message, title: shortcuts + ' · Shift+Enter 换行',
    });
    input.setAttribute('aria-label', message);
    const form = el('form', undefined, 'conversation-composer');
    const controls = el('fieldset');
    const sendLater = el('button', isTask ? '排队发送' : '下一代生效');
    const sendNow = el('button', isTask ? '打断并发送' : '立即生效', 'primary');
    Object.assign(sendLater, {type: 'submit', value: isTask ? 'queue' : 'next'});
    Object.assign(sendNow, {type: 'submit', value: isTask ? 'interrupt' : 'now'});
    sendLater.setAttribute('aria-keyshortcuts', 'Enter');
    sendNow.setAttribute('aria-keyshortcuts', 'Control+Enter Meta+Enter');
    const nowAction = el('span');
    nowAction.append(sendNow);
    const actions = el('div', undefined, 'conversation-compose-actions');
    actions.append(sendLater, nowAction);
    const row = el('div', undefined, 'conversation-compose-row');
    row.append(input, actions);
    controls.append(row, el('small', shortcuts, 'conversation-shortcuts'));
    const feedback = el('p', undefined, 'status-text conversation-status');
    feedback.setAttribute('role', 'status');
    form.append(controls, feedback);
    const footer = el('footer', undefined, 'conversation-ended');
    const endMessage = el('p');
    endMessage.setAttribute('role', 'status');
    footer.append(endMessage);
    mount.classList.add('panel', 'portal-conversation');
    const title = el('h2', '对话');
    title.tabIndex = -1;
    const guidanceChip = el('p', 'agent 正在按你的指引执行', 'conversation-guidance-chip');
    guidanceChip.setAttribute('role', 'status');
    const live = liveStatus({path, active, isTask, logLink, summary, followSummary: update => {
      const follow = atEnd();
      update();
      empty.hidden = true;
      if (follow) scrollToEnd();
      else jump.hidden = false;
    }, interrupt: {
      focus: () => input.focus(),
      available: () => allowed() && !sendNow.disabled,
    }});
    mount.replaceChildren(title, live.strip, scrollArea, guidanceChip, form, footer);

    let cursor, loading, posting, ended = '', paused = false, finishedLoaded = false;
    let turnRunning = false, following = false;
    let lastUpdate;
    const seen = new Set();
    const userMessages = new Map(), pendingReceipts = new Map();
    let lastActivity = null;
    const allowed = () => canWrite() && !ended && !posting && active();
    const pause = action('暂停', 'pause'), resume = action('恢复', 'resume');
    if (!isTask) {
      const steering = el('details', undefined, 'conversation-steering');
      const budget = el('details');
      const field = ui.field('剩余预算（美元）', 'budget', {type: 'number', min: 0, step: 'any'});
      const amount = field.querySelector('input');
      const apply = el('button', '应用预算');
      apply.type = 'button';
      apply.onclick = async () => {
        if (!allowed()) return;
        if (amount.value === '' || !amount.checkValidity() || !Number.isFinite(amount.valueAsNumber)) {
          ui.setMessage(feedback, '请输入大于或等于 0 的剩余预算。', true);
          amount.focus();
          return;
        }
        if (await post({mode: 'budget', remaining_budget_usd: amount.valueAsNumber})) budget.open = false;
      };
      budget.append(el('summary', '调整剩余预算'), ui.actions(field, apply));
      steering.append(el('summary', '运行控制'), ui.actions(pause, resume, action('本代结束后停止', 'stop'),
        action('跳过当前候选', 'skip')), budget);
      controls.prepend(steering);
    }

    function action(label, mode) {
      const node = el('button', label);
      node.type = 'button';
      node.onclick = () => void post({mode});
      return node;
    }

    function setState(reason = '', isPaused = false, running = false, guided = false) {
      if (reason !== ended) finishedLoaded = false;
      ended = reason;
      paused = isPaused;
      turnRunning = running;
      following = guided;
      syncForm();
      live.start();
    }

    function updateTask(task, events) {
      const turns = new Map();
      for (const {type, payload} of events) {
        const candidate = payload.best_of_attempt ?? payload.best_of_candidate;
        const lane = `${payload.subtask ?? ''}/${candidate ?? ''}`;
        if (type === 'agent.turn_started') {
          const mainOrPlanner = candidate == null && (!payload.subtask
            || (payload.turn === 0 && /^plan-\d+$/.test(payload.subtask)));
          if (payload.interjectible || mainOrPlanner) turns.set(lane, payload.following_guidance);
        } else if (['agent.turn_completed', 'agent.turn_interrupted'].includes(type)
            || (type === 'agent.error' && payload.attempt)) {
          turns.delete(lane);
        }
      }
      const running = task.state === 'RUNNING' && turns.size > 0;
      setState(finishedTask(task.state) ? '任务已结束' : '', false, running, [...turns.values()].some(Boolean));
      const update = events.findLast(({type}) => type.startsWith('user.message') || type.startsWith('agent.turn_')
        || type.startsWith('plan.') || type.startsWith('subtask.'));
      if (update && update.seq !== lastUpdate) {
        lastUpdate = update.seq;
        void refresh(true);
      }
    }

    function syncForm() {
      form.hidden = !canWrite() || Boolean(ended);
      controls.disabled = !allowed();
      footer.hidden = !ended;
      endMessage.textContent = ended;
      pause.hidden = paused;
      resume.hidden = !paused;
      guidanceChip.hidden = !isTask || !turnRunning || !following || Boolean(ended);
      if (isTask) {
        sendNow.disabled = !turnRunning;
        nowAction.title = turnRunning ? '' : '当前没有正在执行的轮次，仍可排队发送';
        nowAction.tabIndex = turnRunning ? -1 : 0;
        nowAction.setAttribute('aria-label', nowAction.title);
        if (ended) {
          for (const receipt of userMessages.values()) {
            if (receipt.dataset.status === 'pending') receipt.textContent = '任务已结束 · 未送达';
          }
        }
      }
    }

    function locate(id, label = '定位节点') {
      const link = evidenceLink(label, '#node-' + enc(id));
      link.onclick = event => {
        event.preventDefault();
        if (active()) focus(id);
      };
      return link;
    }

    function userBubble(item, row, body, meta) {
      row.classList.add('conversation-user');
      const content = el('div', undefined, 'conversation-user-body');
      body.append(el('p', item.text));
      if (modes[item.mode]) meta.prepend(el('span', modes[item.mode], 'conversation-tag'));
      if (item.actor) meta.prepend(el('span', item.actor));
      const label = isTask && ended ? '任务已结束 · 未送达' : pendingLabels[item.mode] || '等待处理';
      const receipt = el('span', label, 'conversation-receipt');
      receipt.dataset.status = item.status || 'pending';
      receipt.setAttribute('role', 'status');
      const id = item.message_id || item.id;
      userMessages.set(id, receipt);
      if (pendingReceipts.has(id)) {
        applyReceipt(pendingReceipts.get(id));
        pendingReceipts.delete(id);
      }
      content.append(body, meta, receipt);
      row.append(content);
    }

    function applyReceipt(item) {
      const id = item.message_id;
      if (!id) return false;
      const receipt = userMessages.get(id);
      if (!receipt) {
        pendingReceipts.set(id, item);
        return false;
      }
      const label = item.text;
      receipt.dataset.status = item.status || '';
      receipt.replaceChildren(item.dag_node_id ? locate(item.dag_node_id, label) : el('span', label));
      return true;
    }

    function agentBubble(item, row, body, meta) {
      row.classList.add('conversation-agent');
      const backend = item.backend === 'fake' ? '脚本后端（fake）' : ui.backendLabel(item.backend) || 'Agent';
      const mark = el('span', backend.slice(0, 1), 'conversation-avatar');
      mark.setAttribute('aria-hidden', 'true');
      const group = el('div', undefined, 'conversation-agent-body');
      const name = el('strong', item.agent_label || backend);
      const identity = el('div', undefined, 'conversation-agent-label');
      identity.append(name);
      if (item.scope) identity.append(el('span', item.scope));
      if (item.kind === 'reply') body.append(markdown(item.text));
      else if (item.kind === 'operations') lastActivity = operationCard(body, item.activity);
      else if (item.proposal) body.append(proposalBody(item.proposal));
      else body.append(el('p', item.text, 'conversation-error'));
      group.append(identity, body, meta);
      row.append(mark, group);
    }

    function addItem(item) {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      if (item.kind === 'operations' && lastActivity?.turn === item.activity.turn_seq) {
        lastActivity.append(item.activity);
        return true;
      }
      lastActivity = null;
      if (item.kind === 'receipt') return applyReceipt(item);
      const row = el('li', undefined, 'conversation-row');
      const body = el('div', undefined, 'conversation-bubble');
      const meta = el('div', undefined, 'conversation-meta');
      meta.append(timeLabel(item));
      if (item.duration_s != null) meta.append(el('span', '用时 ' + elapsed(item.duration_s, true)));
      if (item.dag_node_id) meta.append(locate(item.dag_node_id));
      if (item.kind === 'message') userBubble(item, row, body, meta);
      else if (['reply', 'operations', 'error'].includes(item.kind) || item.proposal) {
        agentBubble(item, row, body, meta);
      } else if (item.evaluation) {
        row.classList.add('conversation-evaluation');
        body.append(evaluationBody(item.evaluation), meta);
        row.append(body);
      } else {
        row.classList.add('conversation-system');
        body.classList.add('conversation-chip');
        const decision = ['review.submitted', 'approval.decision'].includes(item.kind);
        const agent = item.agent_label || (item.kind.startsWith('subtask.') && ui.backendLabel(item.backend));
        const waited = ['admission.granted', 'subtask.started'].includes(item.kind);
        const timing = item.duration_s == null ? '' : (waited ? '已等 ' : '用时 ') + elapsed(item.duration_s, true);
        const label = [agent, item.text, timing, item.cost_usd == null ? '' : cost(item.cost_usd)]
          .filter(Boolean).join(' · ');
        body.append(item.dag_node_id ? locate(item.dag_node_id, label) : el('span', label));
        if (decision) {
          body.classList.add('conversation-approval-chip');
          body.append(el('span', '·'));
        }
        body.append(timeLabel(item, decision ? minuteClock : clock));
        if (item.command || item.output_tail) {
          const output = el('details', undefined, 'conversation-step-output');
          output.append(el('summary', item.output_tail ? '查看输出末尾' : '查看命令'));
          if (item.command) output.append(el('code', item.command));
          if (item.output_tail) output.append(el('pre', item.output_tail));
          body.append(output);
        }
        row.append(body);
      }
      list.append(row);
      return true;
    }

    function atEnd() {
      return feed.scrollHeight - feed.clientHeight - feed.scrollTop < 40;
    }

    function scrollToEnd() {
      feed.scrollTop = feed.scrollHeight;
      jump.hidden = true;
    }
    feed.onscroll = () => { if (atEnd()) jump.hidden = true; };
    jump.onclick = () => {
      scrollToEnd();
      list.focus({preventScroll: true});
    };

    function recordDecision(decision, dagNodeId) {
      const follow = atEnd();
      addItem({id: 'decision:' + dagNodeId, kind: 'approval.decision', text: portalApprovals.decisions[decision],
        dag_node_id: dagNodeId, created_at: new Date().toISOString()});
      empty.hidden = true;
      if (follow) scrollToEnd();
      else jump.hidden = false;
    }

    async function refresh(afterCurrent = false) {
      if (!active() || finishedLoaded) return;
      if (loading) {
        if (!afterCurrent) return loading;
        await loading;
        return refresh();
      }
      const terminal = ended;
      loading = (async () => {
        try {
          let more;
          do {
            const page = await data(path + '/conversation?limit=100' + (cursor ? '&cursor=' + enc(cursor) : ''));
            if (!active()) return;
            if (!Array.isArray(page.items)) throw new Error('对话响应不可用');
            const follow = atEnd() || !list.hasChildNodes();
            let changed = false;
            for (const item of page.items) {
              if (addItem(item)) changed = true;
            }
            empty.hidden = list.hasChildNodes() || summary.hasChildNodes();
            if (follow) scrollToEnd();
            else if (changed) jump.hidden = false;
            more = page.has_more && page.cursor !== cursor;
            cursor = page.cursor;
          } while (more);
          loadStatus.textContent = '';
          finishedLoaded = Boolean(terminal) && terminal === ended;
        } catch (error) {
          if (active()) ui.setMessage(loadStatus, error.message || '暂时无法读取对话。', true);
        }
      })();
      try { await loading; } finally { loading = null; }
    }

    async function post(body) {
      if (!allowed()) return false;
      posting = true;
      syncForm();
      ui.setMessage(feedback, '');
      try {
        await data(path + '/messages', body);
        if (!active()) return false;
        await refresh(true);
        return active();
      } catch (error) {
        if (active()) ui.setMessage(feedback, error.message, true);
        return false;
      } finally {
        posting = false;
        if (active()) syncForm();
      }
    }

    form.onsubmit = async event => {
      event.preventDefault();
      if (!allowed() || !input.value.trim()) return;
      const button = event.submitter || sendLater;
      if (button.disabled) return;
      if (await post({text: input.value, mode: button.value})) {
        input.value = '';
        input.oninput?.();
      }
      if (allowed()) input.focus({preventScroll: true});
    };
    if (!globalThis.CSS?.supports('field-sizing: content')) {
      input.oninput = () => {
        const follow = atEnd();
        input.style.blockSize = '';
        input.style.blockSize = input.scrollHeight + input.offsetHeight - input.clientHeight + 'px';
        if (follow) scrollToEnd();
      };
    }
    input.onkeydown = event => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      const button = event.ctrlKey || event.metaKey ? sendNow : sendLater;
      if (allowed() && !button.disabled) form.requestSubmit(button);
    };
    syncForm();
    return {approvals, title, refresh, setState, updateTask, recordDecision, destroy() {
      live.destroy();
      mount.replaceChildren();
    }};
  }

  return {create};
})();

const portalApprovals = (() => {
  const labels = {
    task_plan: '规划审核', task_result: '任务结果审核', task_fix: '修复审核',
    task_selection: 'Best-of-N 候选选择', evolution_candidate: '候选晋升审批', harvest: '采集发布审批',
  };
  const decisions = {approve: '已批准', request_changes: '已要求修改', reject: '已拒绝'};
  const taskKinds = {coding: '编码任务', analysis: '数据分析'};
  const submitted = new Map();
  let identity, nextFetch = 0, pending, rows = [];

  function focus(node) {
    if (!node) return;
    node.focus({preventScroll: true});
    node.scrollIntoView({block: 'center'});
  }

  function card(id, title) {
    const node = el('section', undefined, 'conversation-approval');
    node.id = id;
    node.tabIndex = -1;
    const heading = el('h3', title);
    heading.id = id + '-title';
    node.setAttribute('aria-labelledby', heading.id);
    const status = el('span', undefined, 'conversation-approval-state');
    status.setAttribute('role', 'status');
    const feedback = el('p', undefined, 'status-text conversation-status');
    feedback.setAttribute('role', 'status');
    const header = el('header', undefined, 'conversation-approval-header');
    const summary = el('div', undefined, 'conversation-approval-summary');
    summary.append(heading, status);
    header.append(summary);
    node.append(header);
    return {node, header, status, feedback};
  }

  function setInboxCount(count) {
    $('approvals-count').textContent = count === undefined ? '' : String(count);
    const link = $('approvals-link');
    if (count === undefined) link.removeAttribute('aria-label');
    else link.setAttribute('aria-label', '待审批 ' + count + ' 项');
    portalHome.setApprovals(count);
  }

  function reset() {
    identity = pending = undefined;
    nextFetch = 0;
    rows = [];
    submitted.clear();
    setInboxCount();
    $('approvals-list').replaceChildren();
    $('approvals-status').textContent = '';
  }

  function renderInbox() {
    const list = el('ul', undefined, 'approval-inbox');
    const now = Date.now();
    for (const [href, {item, until}] of submitted) {
      const current = rows.find(row => row.href === href);
      if (!current || until <= now || current.kind !== item.kind
          || (item.kind.startsWith('task_') && current.created_at !== item.created_at)) submitted.delete(href);
    }
    const visible = rows.filter(row => !submitted.has(row.href));
    for (const row of visible) {
      const entry = el('li');
      const details = el('div');
      const context = [labels[row.kind], taskKinds[row.task_kind] || row.task_kind,
        row.backend && portalUI.backendLabel(row.backend), row.project_name].filter(Boolean);
      const title = evidenceLink(row.title, row.href);
      title.className = 'approval-inbox-title';
      title.title = row.title;
      details.append(el('small', context.join(' · ')), title);
      entry.append(details, el('small', portalUI.date(row.created_at)));
      list.append(entry);
    }
    setInboxCount(visible.length);
    $('approvals-list').replaceChildren(visible.length ? list : el('p', '当前没有待审批事项。', 'empty-state'));
  }

  async function submit(path, body, href) {
    const viewer = portalIdentity;
    const item = rows.find(row => row.href === href);
    await api(path, body);
    if (portalIdentity !== viewer) return;
    // Signals can succeed before the workflow leaves review. Hide only the submitted round.
    if (item) {
      submitted.set(href, {item, until: Date.now() + 12000});
      renderInbox();
    }
    void refresh(true);
  }

  function refresh(force = false) {
    if (archivePortal || !portalUI.can('tenant')) return;
    const viewer = portalIdentity;
    if (identity !== viewer) {
      reset();
      identity = viewer;
    }
    if (!force && (pending || Date.now() < nextFetch)) return pending;
    nextFetch = Date.now() + 12000;
    // A refresh after a decision supersedes any inbox request started before it.
    const request = data('/approvals').then(({items}) => {
      if (pending !== request || portalIdentity !== viewer) return;
      rows = items;
      renderInbox();
      if (submitted.size) nextFetch = 0;
      portalUI.setMessage($('approvals-status'), '');
    }).catch(error => {
      if (pending === request && portalIdentity === viewer) {
        portalUI.setMessage($('approvals-status'), error.message, true);
      }
    }).finally(() => {
      if (pending === request) pending = undefined;
    });
    pending = request;
    return request;
  }

  return {card, decisions, focus, refresh, reset, submit};
})();
