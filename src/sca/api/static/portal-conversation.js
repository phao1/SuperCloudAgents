'use strict';

const portalConversation = (() => {
  const ui = portalUI;
  const clock = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});

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
    mount.replaceChildren(el('h2', '对话'), feed, form);

    let cursor, loading, posting, ended = '', paused = false, finishedLoaded = false;
    const seen = new Set();
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

    function bubble(item) {
      const mine = item.kind === 'message';
      const row = el('li', undefined, mine ? 'conversation-row conversation-user' : 'conversation-row');
      const body = el('div', undefined, 'conversation-bubble');
      const meta = el('div', undefined, 'conversation-meta');
      const time = new Date(item.created_at);
      const timestamp = el('time', Number.isFinite(time.valueOf()) ? clock.format(time) : '');
      timestamp.dateTime = item.created_at;
      timestamp.title = ui.date(item.created_at);
      meta.append(timestamp);
      if (mine && item.actor) meta.append(el('span', item.actor));
      if (mine && isTask) meta.append(el('span', item.mode === 'interrupt' ? '打断' : '排队'));
      if (item.dag_node_id) {
        const link = el('a', '定位节点');
        link.href = '#node-' + encodeURIComponent(item.dag_node_id);
        link.onclick = event => {
          event.preventDefault();
          if (active()) focus(item.dag_node_id);
        };
        meta.append(link);
      }
      body.append(el('p', item.text), meta);
      row.append(body);
      return row;
    }

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
            const atEnd = feed.scrollHeight - feed.clientHeight - feed.scrollTop < 40;
            for (const item of page.items) {
              if (seen.has(item.id)) continue;
              seen.add(item.id);
              list.append(bubble(item));
            }
            empty.hidden = seen.size > 0;
            if (atEnd) feed.scrollTop = feed.scrollHeight;
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
    return {refresh, setState, destroy() { mount.replaceChildren(); }};
  }

  return {create};
})();
