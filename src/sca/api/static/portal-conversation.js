'use strict';

const portalConversation = (() => {
  const ui = portalUI;
  const clock = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  const modes = {queue: '排队', interrupt: '打断', guidance: '指引', pause: '暂停', resume: '恢复',
    stop: '停止', skip: '跳过', budget: '预算'};
  const receipts = {pending: '等待送达', delivered: '已送达', applied: '已应用', rejected: '未应用'};

  function timeLabel(item) {
    const time = new Date(item.created_at);
    const timestamp = el('time', Number.isFinite(time.valueOf()) ? clock.format(time) : '');
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

  function create(mount, {path, type, focus, active, canWrite}) {
    const list = el('ol', undefined, 'conversation-items');
    list.setAttribute('role', 'log');
    list.setAttribute('aria-label', '运行进度与消息');
    list.tabIndex = 0;
    const empty = el('p', '等待运行进度…', 'conversation-empty');
    const loadStatus = el('p', undefined, 'status-text conversation-status');
    loadStatus.setAttribute('role', 'status');
    const feed = el('div', undefined, 'conversation-feed');
    feed.append(empty, list, loadStatus);
    const jump = el('button', '有新消息 ↓', 'conversation-jump');
    jump.type = 'button';
    jump.hidden = true;
    const scrollArea = el('div', undefined, 'conversation-scroll-area');
    scrollArea.append(feed, jump);
    const approvals = el('div', undefined, 'conversation-approvals');

    const isTask = type === 'task';
    const message = ui.field(isTask ? '插话' : '给下一代提议的指引', 'text', {
      multiline: true, required: true, rows: 2, maxLength: 4000,
      placeholder: '回车发送，Shift + 回车换行',
    });
    const input = message.querySelector('textarea');
    const form = el('form', undefined, 'stack conversation-composer');
    const controls = el('fieldset', undefined, 'stack');
    const send = el('button', '发送', 'primary');
    send.type = 'submit';
    const mode = isTask ? ui.field('送达方式', 'mode', {
      value: 'queue', choices: [['queue', '排队'], ['interrupt', '打断']],
    }) : null;
    controls.append(message, ui.actions(mode, send));
    const feedback = el('p', undefined, 'status-text conversation-status');
    feedback.setAttribute('role', 'status');
    const disabled = el('p', undefined, 'status-text conversation-status');
    form.append(controls, disabled, feedback);
    mount.classList.add('panel', 'portal-conversation');
    const title = el('h2', '对话');
    title.tabIndex = -1;
    mount.replaceChildren(title, approvals, scrollArea, form);

    let cursor, loading, posting, ended = '', paused = false, finishedLoaded = false;
    const seen = new Set();
    const userMessages = new Map(), pendingReceipts = new Map();
    let lastActivity = null;
    const allowed = () => canWrite() && !ended && !posting && active();
    const pause = action('暂停', 'pause'), resume = action('恢复', 'resume');
    if (!isTask) {
      const budget = el('details', undefined, 'conversation-budget');
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
      controls.prepend(ui.actions(pause, resume, action('本代结束后停止', 'stop'),
        action('跳过当前候选', 'skip')), budget);
    }

    function action(label, mode) {
      const node = el('button', label);
      node.type = 'button';
      node.onclick = () => void post({mode});
      return node;
    }

    function setState(reason = '', isPaused = false) {
      if (reason !== ended) finishedLoaded = false;
      ended = reason;
      paused = isPaused;
      syncForm();
    }

    function syncForm() {
      form.hidden = !canWrite();
      controls.disabled = !allowed();
      disabled.textContent = ended;
      pause.hidden = paused;
      resume.hidden = !paused;
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
      const receipt = el('span', receipts[item.status] || receipts.pending, 'conversation-receipt');
      receipt.dataset.status = item.status || 'pending';
      meta.append(receipt);
      const id = item.message_id || item.id;
      userMessages.set(id, receipt);
      if (pendingReceipts.has(id)) {
        applyReceipt(pendingReceipts.get(id));
        pendingReceipts.delete(id);
      }
      content.append(body, meta);
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
      const label = item.text || receipts[item.status] || '';
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
      const name = el('strong', backend);
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
        body.append(item.dag_node_id ? locate(item.dag_node_id, item.text) : el('span', item.text), timeLabel(item));
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

    async function refresh() {
      if (!active() || finishedLoaded) return;
      if (loading) return loading;
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
            empty.hidden = list.hasChildNodes();
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
      ui.setMessage(feedback, '正在发送…');
      try {
        await data(path + '/messages', body);
        if (!active()) return false;
        ui.setMessage(feedback, '已发送，等待回执。');
        await refresh();
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
      const value = isTask ? ui.value(form, 'mode') : 'guidance';
      if (await post({text: input.value, mode: value})) input.value = '';
      if (allowed()) input.focus({preventScroll: true});
    };
    input.onkeydown = event => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      if (allowed()) form.requestSubmit();
    };
    syncForm();
    return {approvals, title, refresh, setState, destroy() { mount.replaceChildren(); }};
  }

  return {create};
})();

const portalApprovals = (() => {
  const labels = {
    task_plan: '规划审核', task_result: '任务结果审核', task_fix: '修复审核',
    task_selection: 'Best-of-N 候选选择', evolution_candidate: '候选晋升审批', harvest: '采集发布审批',
  };
  const decisions = {approve: '已批准', request_changes: '已要求修改', reject: '已拒绝'};
  let identity, nextFetch = 0, pending;

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
    const status = el('p', undefined, 'conversation-approval-state');
    status.setAttribute('role', 'status');
    const feedback = el('p', undefined, 'status-text conversation-status');
    feedback.setAttribute('role', 'status');
    node.append(heading, status);
    return {node, status, feedback};
  }

  function reset() {
    identity = pending = undefined;
    nextFetch = 0;
    $('approvals-count').textContent = '';
    $('approvals-list').replaceChildren();
    $('approvals-status').textContent = '';
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
    const request = data('/approvals').then(({count, items}) => {
      if (pending !== request || portalIdentity !== viewer) return;
      $('approvals-count').textContent = ' (' + count + ')';
      const list = el('ul', undefined, 'approval-inbox');
      for (const row of items) {
        const entry = el('li');
        const details = el('div');
        details.append(el('small', labels[row.kind]), evidenceLink(row.title, row.href));
        entry.append(details, el('small', portalUI.date(row.created_at)));
        list.append(entry);
      }
      $('approvals-list').replaceChildren(items.length ? list : el('p', '当前没有待审批事项。', 'empty-state'));
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

  return {card, decisions, focus, refresh, reset};
})();
