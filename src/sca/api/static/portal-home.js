'use strict';

const portalHome = (() => {
  const running = new Set(['PROVISIONING', 'RUNNING', 'VERIFYING']);

  function canShowEvolutions() {
    return portalUI.can('eval-history') && portalUI.can('evolution-history');
  }

  function sync() {
    $('home-connect').hidden = Boolean(portalIdentity);
    $('home-evolution-panel').hidden = !canShowEvolutions();
  }

  function setApprovals(count) {
    $('home-approvals').textContent = String(count ?? '—');
  }

  function setModel(config) {
    $('home-model-status').textContent = config?.model_configured === false ? '未配置'
      : config?.model_configured === true ? '已配置' : '—';
  }

  function list(target, rows, empty) {
    const signature = JSON.stringify(rows);
    if (target.dataset.rows === signature) return;
    target.dataset.rows = signature;
    if (!rows.length) {
      target.replaceChildren(el('p', empty));
      return;
    }
    const entries = el('ul', undefined, 'home-list');
    for (const {title, href, updated, state} of rows) {
      const row = el('li'), text = el('div');
      text.append(evidenceLink(title, href));
      if (updated) text.append(el('small', portalUI.date(updated)));
      row.append(text, status(state));
      entries.append(row);
    }
    target.replaceChildren(entries);
  }

  async function load(polling) {
    if (!portalUI.can('history')) return;
    const hash = location.hash, identity = portalIdentity;
    const current = () => hash === location.hash && identity === portalIdentity;
    const today = new Date().toISOString().slice(0, 10);
    await Promise.all([
      loadRecentTasks().then(tasks => {
        if (!current() || !tasks) return;
        $('home-running').textContent = String(tasks.filter(task => running.has(task.state)).length);
        list($('home-tasks'), tasks.slice(0, 5).map(task => ({
          title: (taskCase(task) || (task.spec?.instruction || task.spec?.question || '').trim().split('\n')[0])
            .slice(0, 96) || task.id.slice(0, 8),
          href: '#/tasks/' + enc(task.id), updated: task.updated_at, state: task.state,
        })), '暂无任务。');
      }),
      !identity.guest && (polling || !portalConfig) ? loadConfig() : null,
      portalUI.can('admin') ? data('/usage?from=' + today + '&to=' + today).then(rows => {
        if (!current()) return;
        const costs = rows.map(row => row.cost_usd).filter(Number.isFinite);
        $('home-spend').textContent = costs.length || !rows.length ? money(costs.reduce((a, b) => a + b, 0)) : '—';
      }).catch(() => { if (current()) $('home-spend').textContent = '—'; }) : null,
      canShowEvolutions() ? data('/evolutions').then(rows => {
        if (!current()) return;
        const recent = [...rows.filter(row => portalEvolution.isActive(row.status)),
          ...rows.filter(row => !portalEvolution.isActive(row.status))].slice(0, 5);
        list($('home-evolutions'), recent.map(row => ({
          title: portalEvolution.suiteLabel(row.config.suite) + ' · ' + row.id.slice(0, 8),
          href: '#/evolutions/runs/' + enc(row.id), updated: row.created_at, state: row.status,
        })), '暂无演进运行。');
      }) : null,
    ]);
  }

  function reset() {
    setApprovals();
    setModel();
    for (const id of ['home-running', 'home-spend']) $(id).textContent = '—';
    for (const id of ['home-tasks', 'home-evolutions']) {
      $(id).replaceChildren();
      delete $(id).dataset.rows;
    }
  }

  return {load, reset, setApprovals, setModel, sync};
})();
