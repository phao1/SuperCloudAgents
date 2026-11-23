'use strict';

const portalUI = (() => {
  function backendLabel(name) {
    return {codex: 'Codex', opencode: 'OpenCode', 'opencode-brain': 'OpenCode（脑手分离）',
      pi: 'Pi', dsh: 'DeepSeek Harness', fake: '脚本后端'}[name] || name;
  }

  function backendChoices(config, selected = '') {
    const names = new Set(config.agent_backends);
    if (selected) names.add(selected);
    return [['', '默认 · ' + backendLabel(config.default_agent_backend)],
      ...Array.from(names, name => [name, backendLabel(name)])];
  }

  function can(access, identity = portalIdentity) {
    const history = Boolean(identity?.tenant_id && !identity.platform_admin);
    const tenant = history && !identity.guest;
    const admin = tenant && identity.role === 'admin';
    const evaluation = tenant && identity.tenant_id === 'eval';
    return ({
      public: true, history, tenant, admin, eval: evaluation, 'eval-admin': evaluation && admin,
      'eval-history': history && identity.tenant_id === 'eval',
      'evolution-history': admin || (history && identity.tenant_id === 'eval' && (identity.guest || identity.role === 'member')),
      member: tenant && ['member', 'admin'].includes(identity.role),
      platform: identity?.platform_admin === true,
    })[access] === true;
  }

  function field(label, name, config = {}) {
    const {choices, note, multiline, wide, ...properties} = config;
    const wrap = el('label', label, wide ? 'field-wide' : undefined);
    const input = el(choices ? 'select' : multiline ? 'textarea' : 'input');
    input.name = name;
    if (choices) {
      input.append(...choices.map(([value, text]) => {
        const option = el('option', text);
        option.value = value;
        option.defaultSelected = properties.value !== undefined && String(value) === String(properties.value);
        return option;
      }));
    } else if (properties.value !== undefined) {
      input.defaultValue = properties.value;
    }
    Object.assign(input, properties);
    wrap.append(input);
    if (note) wrap.append(el('small', note));
    return wrap;
  }

  function fields(...items) {
    const group = el('div', undefined, 'fields two');
    group.append(...items);
    return group;
  }

  function control(form, name) {
    return form.elements.namedItem(name);
  }

  function value(form, name) {
    return control(form, name).value.trim();
  }

  function checked(form, name) {
    return control(form, name).checked;
  }

  function check(label, name, checked = false) {
    const wrap = el('label', undefined, 'portal-check');
    const input = el('input');
    Object.assign(input, {type: 'checkbox', name, checked, defaultChecked: checked});
    wrap.append(input, document.createTextNode(label));
    return wrap;
  }

  function form(content, submitLabel, submit) {
    const node = el('form', undefined, 'stack');
    const actions = el('div', undefined, 'form-actions');
    const send = el('button', submitLabel, 'primary');
    send.type = 'submit';
    actions.append(send);
    const status = el('p', undefined, 'status-text portal-message');
    status.setAttribute('role', 'status');
    node.append(...content, actions, status);
    node.onsubmit = async event => {
      event.preventDefault();
      if (send.disabled) return;
      send.disabled = true;
      setMessage(status, '');
      try {
        const message = await submit(node);
        if (message) setMessage(status, message);
      } catch (e) {
        setMessage(status, e.message || String(e), true);
      } finally {
        send.disabled = false;
      }
    };
    return node;
  }

  function setMessage(node, message, problem = false) {
    node.textContent = message;
    node.classList.toggle('portal-problem', problem);
  }

  function actions(...items) {
    const group = el('div', undefined, 'form-actions portal-actions');
    group.append(...items.filter(Boolean));
    return group;
  }

  function action(label, callback, message) {
    const node = button(label, async () => {
      if (node.disabled) return;
      node.disabled = true;
      if (message) setMessage(message, '');
      try {
        await callback();
      } catch (e) {
        if (message) setMessage(message, e.message || String(e), true);
        else error(e);
      } finally {
        node.disabled = false;
      }
    });
    return node;
  }

  function list(target, headers, rows, empty = '暂无记录。') {
    target.replaceChildren(rows.length ? table(headers, rows) : el('p', empty, 'empty-state'));
  }

  function panel(title, ...content) {
    const node = el('section', undefined, 'panel');
    node.append(el('h2', title));
    for (const child of content) {
      if (child.tagName === 'TABLE') {
        const view = scroll(title);
        view.append(child);
        node.append(view);
      } else {
        node.append(child);
      }
    }
    return node;
  }

  function scroll(label) {
    const node = el('div', undefined, 'scroll');
    node.tabIndex = 0;
    node.setAttribute('role', 'region');
    node.setAttribute('aria-label', label);
    return node;
  }

  function lines(value) {
    return value.split(/\r?\n/u).map(line => line.trim()).filter(Boolean);
  }

  function items(value) {
    return value.split(/[,，\s]+/u).filter(Boolean);
  }

  function object(value, label) {
    if (!value) return {};
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
    } catch {
      // Report the field name without echoing the parsed input.
    }
    throw Error(label + '必须是 JSON 对象。');
  }

  function safeURL(raw, relative = false) {
    if (typeof raw !== 'string') return null;
    const value = raw.trim();
    if (!value || /[\s\u0000-\u001f\u007f\\]/u.test(value) || value.startsWith('//')) return null;
    if (!relative && !/^https?:\/\//iu.test(value)) return null;
    try {
      const url = new URL(value, location.href);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password
        ? url.href : null;
    } catch {
      return null;
    }
  }

  function externalLink(label, raw, relative = false) {
    const url = safeURL(raw, relative);
    if (!url) return el('span', label);
    const link = evidenceLink(label, url);
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  }

  function date(value) {
    return value ? new Date(value).toLocaleString('zh-CN') : '—';
  }

  function percent(value) {
    return value === null || value === undefined ? '—' : (100 * Number(value)).toFixed(1) + '%';
  }

  function cost(value) {
    return value === null || value === undefined ? '—' : money(value);
  }

  async function upload(file) {
    const form = new FormData();
    form.append('file', file);
    const response = await fetch('/v1/files', {
      method: 'POST', headers: authHeaders(),
      body: form, signal: AbortSignal.timeout(120000),
    });
    if (!response.ok) throw await apiFailure(response);
    return response.json();
  }

  async function copy(value, input) {
    if (navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch {
        // Plain HTTP deployments can select the field for manual copying.
      }
    }
    input.focus();
    input.select();
    return document.execCommand?.('copy') === true;
  }

  function svg(tag, attributes, title) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    if (title !== undefined) node.textContent = title;
    return node;
  }

  return {
    action, actions, backendChoices, backendLabel, can, check, checked, control, copy, cost,
    date, externalLink, field, fields,
    form, items, lines, list, object, panel, percent, scroll, setMessage, svg, upload, value,
  };
})();
