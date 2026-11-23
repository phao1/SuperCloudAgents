'use strict';

const portalEvolution = (() => {
  const ui = portalUI;
  const runsPath = '/evolutions', schedulesPath = '/evolution-schedules';
  const championsPath = '/champions', rolloutsPath = '/champions/rollouts', harvestPath = '/evals/harvest';
  const taskHref = id => '#/tasks/' + enc(id);
  const runHref = id => '#/runs/' + enc(id);
  const pageHref = (tab, id) => '#/evolutions/' + tab + (id ? '/' + enc(id) : '');
  const short = value => String(value || '').slice(0, 8);
  const activeStates = ['queued', 'running', 'paused', 'awaiting_approval'];
  const launchDefaults = {generations: 1, candidates: 2, repeat: 1, budget_usd: 1};
  const launchFields = new Set(['generations', 'candidates', 'budget_usd']);
  const surfaces = [['system_prompt', '系统提示词'], ['tool_guidance', '工具指引'], ['tool_descriptions', '工具描述']];
  const optionFields = [
    ['generations', '最多代数', 3, 1, 100], ['candidates', '每代候选数', 3, 1, 10],
    ['repeat', '重复次数', 2, 1, 100], ['concurrency', '评测并发', 1, 1, 100],
    ['budget_usd', '总预算（美元）', 3, 0, null, 'any'],
    ['case_budget', '单例预算（美元）', .05, 0, null, 'any'],
    ['patience', '连续未晋升停止代数', 3, 1, 100],
    ['max_cost_increase', '允许的成本涨幅（比例）', .25, 0, null, 'any'],
    ['min_cost_saving', '最低节省成本（比例）', .15, 0, 1, 'any'],
  ];
  const stateNames = {
    awaiting_approval: '待审批', paused: '已暂停', stopped: '已停止', patience_exhausted: '达到停滞上限',
    pending: '待定', promoted: '已晋升', invalid: '静态检查未通过', rejected: '已拒绝',
    approved: '已批准',
  };
  const reasons = {
    'awaiting approval': '待人工审批',
    'static checks failed': '静态检查未通过',
    'evaluation stopped': '评测已停止',
    'not selected or dev gate failed': '未入选或未通过开发集门禁',
    'holdout gate passed (quality)': '质量提升，通过留出集门禁',
    'holdout gate passed (efficiency)': '效率提升，通过留出集门禁',
    'quality win on dev; holdout gate or approval rejected': '开发集质量提升，留出集门禁或审批未通过',
    'efficiency win on dev; holdout gate or approval rejected': '开发集效率提升，留出集门禁或审批未通过',
  };
  let view = null;

  function stateLabel(value) {
    return stateNames[value] || statusText(value);
  }

  function notice() {
    const node = el('p', undefined, 'status-text portal-message');
    node.setAttribute('role', 'status');
    return node;
  }

  function active(state) {
    return view === state && state.identity === portalIdentity && location.hash.startsWith('#/evolutions');
  }

  function detailLink(tab, id, label = short(id)) {
    return id ? evidenceLink(label, pageHref(tab, id)) : '—';
  }

  function suiteLabel(name) {
    return name?.split(/[/\\]/).at(-1) || '—';
  }

  function configFields(config = {}, launch = false) {
    const basic = [], advanced = [];
    for (const [name, label, fallback, min, max, step] of optionFields) {
      const fields = launch && launchFields.has(name) ? basic : advanced;
      fields.push(ui.field(label, name, {
        type: 'number', value: config[name] ?? fallback, min, ...(max !== null && {max}),
        step: step || 1, required: true,
      }));
    }
    const toggles = surfaces.map(([name, label]) => ui.check(label, 'surface_' + name,
      !config.surfaces || config.surfaces.includes(name)));
    const details = el('details', undefined, 'portal-disclosure');
    details.append(el('summary', launch ? '高级选项与晋升条件' : '候选、预算与晋升条件'), ui.fields(...advanced),
      ui.actions(...toggles), ui.check('晋升前需要人工审批', 'require_approval', config.require_approval === true));
    if (!launch) return details;
    const section = el('div', undefined, 'stack');
    section.append(ui.fields(...basic), details);
    return section;
  }

  function configBody(form) {
    const config = Object.fromEntries(optionFields.map(([name]) => [name, Number(ui.value(form, name))]));
    config.require_approval = ui.checked(form, 'require_approval');
    config.surfaces = surfaces.map(([name]) => name).filter(name => ui.checked(form, 'surface_' + name));
    config.evolve_model = ui.value(form, 'evolve_model') || null;
    return config;
  }

  function backendField(value = 'opencode') {
    return ui.field('Agent 后端', 'agent_backend', {value,
      choices: ['opencode', 'dsh'].map(name => [name, ui.backendLabel(name)])});
  }

  function backendLabel(row) {
    return ui.backendLabel(row.agent_backend || 'opencode');
  }

  function targetLabel(row) {
    return [backendLabel(row), row.agent_version, row.model].join(' · ');
  }

  function targetFields(row = {}) {
    const installed = new Map();
    for (const suite of suites) {
      const counts = {dev: 0, holdout: 0};
      for (const {split} of Object.values(suite.case_metadata || {})) {
        if (Object.hasOwn(counts, split)) counts[split]++;
      }
      if (counts.dev && counts.holdout) installed.set(suite.name,
        `${suite.name} · 开发集 ${counts.dev} · 留出集 ${counts.holdout}`);
    }
    if (row.suite && !installed.has(row.suite)) installed.set(row.suite, row.suite);
    let selected = row.agent_backend || (portalConfig?.default_agent_backend === 'dsh' ? 'dsh' : 'opencode');
    const recommended = backend => {
      const preference = backend === 'dsh' ? 'dsh-smoke' : 'opencode-evolve';
      return installed.has(preference) ? preference : installed.keys().next().value || '';
    };
    const choices = [...installed];
    if (!choices.length) choices.push(['', '暂无含开发集和留出集的套件']);
    const pins = {opencode: versions[0]?.version || '', dsh: ''};
    if (row.agent_version) pins[selected] = row.agent_version;
    const suiteField = ui.field('评测套件', 'suite', {
      required: true, value: row.suite || recommended(selected), choices,
    });
    const result = ui.fields(
      backendField(selected), suiteField,
      ui.field('基线版本', 'agent_version', {value: pins[selected]}),
      portalModels.field('评测模型', 'model', {required: true, value: row.model || portalConfig?.default_model || ''}),
      portalModels.field('反思与候选生成模型', 'evolve_model', {
        value: row.config?.evolve_model || portalConfig?.evolve_model || '',
        placeholder: portalConfig?.default_model || '',
      })
    );
    const backend = result.querySelector('[name="agent_backend"]');
    const version = result.querySelector('[name="agent_version"]');
    const suite = suiteField.querySelector('select');
    let suitePicked = Boolean(row.suite);
    suite.onchange = () => {suitePicked = true;};
    function syncVersion() {
      version.required = selected === 'opencode';
      version.placeholder = selected === 'dsh' ? '留空使用已配置的 npm 版本' : '支持运行时覆盖的版本';
    }
    backend.onchange = () => {
      pins[selected] = version.value;
      selected = backend.value;
      version.value = pins[selected];
      if (!suitePicked) suite.value = recommended(selected);
      syncVersion();
    };
    syncVersion();
    return result;
  }

  function evolutionBudget(form) {
    portalModels.bindBudget(form, () => [ui.value(form, 'model'),
      ui.value(form, 'evolve_model') || portalConfig?.evolve_model]);
  }

  function targetBody(form) {
    return {suite: ui.value(form, 'suite'), agent_backend: ui.value(form, 'agent_backend'),
      agent_version: ui.value(form, 'agent_version') || null, model: ui.value(form, 'model')};
  }

  function loadTargets(state, ready) {
    void Promise.allSettled([loadSuites(), loadVersions(), portalConfig ? null : loadConfig(), data('/models')]).then(results => {
      if (!active(state)) return;
      portalModels.setCatalog(results[3].value);
      ready(results.find(result => result.status === 'rejected')?.reason);
    });
  }

  function mountRuns(state) {
    const list = ui.scroll('演进记录');
    if (ui.can('eval-admin')) {
      const launch = el('div', undefined, 'stack'), message = notice();
      ui.setMessage(message, '正在读取评测套件和后端配置…');
      state.root.append(ui.panel('启动自动演进', launch, message));
      loadTargets(state, problem => {
        const form = ui.form([targetFields(), configFields(launchDefaults, true)], '开始演进', async form => {
          const result = await data(runsPath, {...targetBody(form), ...configBody(form)});
          if (active(state)) location.hash = pageHref('runs', result.id);
        });
        evolutionBudget(form);
        launch.append(form);
        ui.setMessage(message, problem?.message || '', Boolean(problem));
      });
    }
    state.root.append(ui.panel('演进记录', list));
    let signature;
    return async () => {
      const rows = await data(runsPath);
      const next = JSON.stringify(rows);
      if (signature === next) return;
      signature = next;
      ui.list(list, ['套件 / 演进 ID', '后端 / 基线版本 / 模型', '状态', '支出 / 预算', '创建时间'], rows.map(row => {
        const state = el('div', stateLabel(row.status), 'stack');
        if (row.config.reason) state.append(el('small', row.config.reason, 'bad portal-reason'));
        return [detailLink('runs', row.id, suiteLabel(row.config.suite) + ' · ' + short(row.id)),
          targetLabel(row.config), state, ui.cost(row.spent_usd) + ' / ' + ui.cost(row.config.budget_usd),
          ui.date(row.created_at)];
      }), '尚无自动演进。');
    };
  }

  function metricLine(metrics, runId) {
    if (!metrics) return '—';
    const text = [
      ui.percent(metrics.pass_rate), ui.cost(metrics.cost_usd),
      metrics.unpriced ? Number(metrics.tokens).toLocaleString('zh-CN') + ' Token' : null,
      metrics.turns === undefined ? null : Number(metrics.turns).toFixed(1) + ' 轮',
    ].filter(Boolean).join(' · ');
    return runId ? evidenceLink(text, runHref(runId)) : text;
  }

  function overlayFields(overlay) {
    const fields = new Map();
    if (overlay.system_prompt != null) fields.set('系统提示词', overlay.system_prompt);
    if (overlay.tool_guidance != null) fields.set('工具指引', overlay.tool_guidance);
    for (const key of Object.keys(overlay.tool_descriptions || {}).sort()) {
      fields.set('工具描述 · ' + key, overlay.tool_descriptions[key]);
    }
    return fields;
  }

  function lineChanges(before, after) {
    const a = before === undefined ? [] : before.split('\n');
    const b = after === undefined ? [] : after.split('\n');
    let first = 0, aEnd = a.length, bEnd = b.length;
    while (first < aEnd && first < bEnd && a[first] === b[first]) first++;
    while (aEnd > first && bEnd > first && a[aEnd - 1] === b[bEnd - 1]) {
      aEnd--;
      bEnd--;
    }
    const x = a.slice(first, aEnd), y = b.slice(first, bEnd);
    const changes = a.slice(Math.max(0, first - 3), first).map(line => ' ' + line);
    if ((x.length + 1) * (y.length + 1) > 250000) {
      for (const line of x) changes.push('-' + line);
      for (const line of y) changes.push('+' + line);
    } else {
      const scores = Array.from({length: x.length + 1}, () => new Uint32Array(y.length + 1));
      for (let i = x.length - 1; i >= 0; i--) {
        for (let j = y.length - 1; j >= 0; j--) {
          scores[i][j] = x[i] === y[j] ? 1 + scores[i + 1][j + 1] : Math.max(scores[i + 1][j], scores[i][j + 1]);
        }
      }
      let i = 0, j = 0;
      while (i < x.length || j < y.length) {
        if (i < x.length && j < y.length && x[i] === y[j]) {
          changes.push(' ' + x[i++]);
          j++;
        } else if (i < x.length && (j === y.length || scores[i + 1][j] >= scores[i][j + 1])) {
          changes.push('-' + x[i++]);
        } else {
          changes.push('+' + y[j++]);
        }
      }
    }
    changes.push(...a.slice(aEnd, aEnd + 3).map(line => ' ' + line));
    return changes;
  }

  function overlayDiff(parent, current) {
    const a = overlayFields(parent), b = overlayFields(current);
    const output = el('div', undefined, 'diff');
    for (const field of new Set([...a.keys(), ...b.keys()])) {
      if (a.get(field) === b.get(field)) continue;
      output.append(el('span', '@@ ' + field + ' @@'));
      for (const line of lineChanges(a.get(field), b.get(field))) {
        output.append(el('span', line, line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : ''));
      }
    }
    return output.hasChildNodes() ? output : el('small', '覆盖内容与父候选一致。');
  }

  function generations(candidates, initial, baselineRun) {
    const groups = new Map();
    for (const candidate of candidates) {
      if (!groups.has(candidate.generation)) groups.set(candidate.generation, []);
      groups.get(candidate.generation).push(candidate);
    }
    let champion = initial, run = baselineRun;
    return [...groups.entries()].sort(([a], [b]) => a - b).map(([number, group]) => {
      const row = {number, champion, run, candidates: group};
      const next = group.find(candidate => candidate.decision === 'promoted' && candidate.dev_metrics);
      if (next) {
        champion = next.dev_metrics;
        run = next.dev_run_id;
      }
      return row;
    });
  }

  function chart(groups, key, label, format) {
    const plot = ui.svg('svg', {viewBox: '0 0 560 165', role: 'img', 'aria-label': label, class: 'portal-chart'});
    const maximum = key === 'pass_rate' ? 1 : Math.max(.01, ...groups.flatMap(group =>
      [group.champion?.[key] || 0, ...group.candidates.map(item => item.dev_metrics?.[key] || 0)]));
    const x = i => 73 + 457 * (groups.length === 1 ? .5 : i / (groups.length - 1));
    const y = value => 136 - 115 * Number(value) / maximum;
    for (const tick of [0, .25, .5, .75, 1]) {
      const mark = tick * maximum;
      plot.append(ui.svg('line', {x1: 72, x2: 536, y1: y(mark), y2: y(mark), class: 'axis'}),
        ui.svg('text', {x: 65, y: y(mark) + 4, 'text-anchor': 'end'}, format(mark)));
    }
    const points = groups.flatMap((group, i) => group.champion?.[key] == null ? []
      : [x(i) + ',' + y(group.champion[key])]);
    plot.append(ui.svg('polyline', {points: points.join(' '), class: 'champion'}));
    groups.forEach((group, i) => {
      const center = x(i);
      if (i % Math.ceil(groups.length / 12) === 0 || i === groups.length - 1) {
        plot.append(ui.svg('text', {x: center, y: 155, 'text-anchor': 'middle'}, '第 ' + group.number + ' 代'));
      }
      const candidates = group.candidates.filter(item => item.dev_metrics?.[key] != null);
      candidates.forEach((item, j) => {
        const point = ui.svg('circle', {
          cx: center + (j - (candidates.length - 1) / 2) * Math.min(5, 105 / groups.length),
          cy: y(item.dev_metrics[key]), r: 3.6, class: 'candidate',
        });
        point.append(ui.svg('title', {}, '第 ' + group.number + ' 代候选 ' + short(item.id) + ' · ' + format(item.dev_metrics[key])));
        plot.append(point);
      });
      if (group.champion?.[key] != null) {
        const point = ui.svg('rect', {x: center - 3, y: y(group.champion[key]) - 3, width: 6,
          height: 6, class: 'champion'});
        point.append(ui.svg('title', {}, '第 ' + group.number + ' 代冠军 · ' + format(group.champion[key])));
        plot.append(point);
      }
    });
    const wrap = ui.scroll(label);
    wrap.append(plot);
    const panel = el('div', undefined, 'stack');
    panel.append(el('h3', label), wrap);
    return panel;
  }

  function mountRun(state) {
    const id = state.id, path = runsPath + '/' + enc(id);
    const canManage = ui.can('eval-admin');
    const canReadChampions = canManage || state.identity.guest;
    const header = el('div', undefined, 'stack'), spend = el('p');
    const actions = ui.actions(evidenceLink('返回演进记录', pageHref('runs')));
    const message = notice(), plots = el('div', undefined, 'portal-charts'), baselineList = ui.scroll('各代冠军');
    const candidates = ui.scroll('演进候选');
    candidates.id = 'evolution-candidates';
    const legend = el('div', undefined, 'portal-chart-note');
    legend.append(el('span', '当代冠军'), el('span', '候选', 'candidate'));
    const panel = $('evolution-dag-template').content.firstElementChild.cloneNode(true);
    let currentRun, liveSpend;
    function updateSpend() {
      if (!currentRun) return;
      const total = Math.max(Number(currentRun.spent_usd) || 0, liveSpend ?? 0);
      spend.textContent = stateLabel(currentRun.status) + ' · 已用 ' + ui.cost(total)
        + ' · 评测预算 ' + ui.cost(currentRun.config.budget_usd);
    }
    state.dag = SCATaskDAG.create(panel, async runId => {
      const graph = await data(runsPath + '/' + enc(runId) + '/dag');
      if (active(state)) {
        liveSpend = graph.nodes?.find(node => node.kind === 'evolution')?.cost_usd;
        updateSpend();
      }
      return graph;
    }, {
      project: SCATaskDAG.projectEvolution,
    });
    const workbench = el('div', undefined, 'portal-run-workbench');
    const conversation = el('section');
    conversation.id = 'evolution-conversation';
    conversation.setAttribute('aria-label', '演进对话');
    workbench.append(panel, conversation);
    state.conversation = portalConversation.create(conversation, {
      path, type: 'evolution', active: () => active(state),
      canWrite: () => ui.can('member'), focus: node => state.dag.focus(node),
    });
    state.root.append(ui.panel('演进详情', header, actions, message), workbench,
      ui.panel('每代开发集成绩', legend, plots, baselineList), ui.panel('候选与晋升决定', candidates));
    const approvalCards = new Map();

    function pendingApproval(run, candidate) {
      return run.status === 'awaiting_approval' && candidate.decision === 'pending'
        && candidate.reason === 'awaiting approval';
    }

    function updateApprovals(run) {
      for (const candidate of run.candidates) {
        const awaiting = pendingApproval(run, candidate);
        let approval = approvalCards.get(candidate.id);
        if (!approval && !awaiting) continue;
        if (!approval) {
          const card = portalApprovals.card('candidate-approval-' + candidate.id, '候选晋升审批 · 第 ' + candidate.generation + ' 代');
          const nodeId = 'candidate:' + candidate.id;
          const href = pageHref('runs', id) + '/approval/' + enc(candidate.id);
          const locate = evidenceLink('定位候选节点', '#node-' + enc(nodeId));
          locate.onclick = event => {
            event.preventDefault();
            state.dag.focus(nodeId);
          };
          const controls = el('fieldset');
          const app = approval = {...card, controls};
          if (canManage) controls.append(ui.actions(...[['approve', '批准'], ['reject', '拒绝']].map(([action, label]) =>
            ui.action(label, async () => {
              if (!canManage || !active(state) || app.sent) return;
              controls.disabled = true;
              try {
                await portalApprovals.submit(path + '/' + action, {candidate_id: candidate.id}, href);
                if (!active(state)) return;
                app.sent = true;
                if (card.node.contains(document.activeElement)) state.conversation.title.focus({preventScroll: true});
                card.node.hidden = true;
                state.conversation.recordDecision(action, nodeId);
                await refresh(state);
              } finally {
                controls.disabled = false;
              }
            }, card.feedback))));
          const detail = el('details');
          const evidence = el('div', undefined, 'conversation-approval-evidence');
          evidence.append(el('p', candidate.hypothesis), locate, ui.actions(
            el('span', '开发集'), metricLine(candidate.dev_metrics, candidate.dev_run_id),
            el('span', '留出集'), metricLine(candidate.holdout_metrics, candidate.holdout_run_id)));
          detail.append(el('summary', '候选详情'), evidence);
          card.header.append(controls);
          card.node.append(detail, card.feedback);
          approvalCards.set(candidate.id, approval);
          state.conversation.approvals.append(card.node);
        }
        approval.node.hidden = !awaiting || Boolean(approval.sent);
        approval.controls.hidden = !canManage;
        approval.status.textContent = canManage ? '等待人工审批' : '等待人工审批 · 只读';
      }
      if (state.approval && state.focusedApproval !== state.approval) {
        const card = approvalCards.get(state.approval)?.node;
        portalApprovals.focus(card && !card.hidden ? card : state.conversation.title);
        state.focusedApproval = state.approval;
      }
    }

    let signature, initial, parents;
    return async () => {
      const row = await data(path);
      if (!active(state)) return;
      currentRun = row;
      updateSpend();
      state.dag.refresh(id, true);
      state.polling = activeStates.includes(row.status);
      state.conversation.setState(state.polling ? '' : '演进已结束', row.status === 'paused');
      updateApprovals(row);
      void state.conversation.refresh();
      const nextSignature = JSON.stringify([row.status, row.reason, row.spent_usd, row.best_champion_id,
        row.config.budget_usd,
        row.candidates.map(c => [c.id, c.decision, c.reason, c.dev_run_id, c.holdout_run_id]),
        row.journal.filter(entry => entry.type === 'evaluation').map(entry => [entry.id, entry.status])]);
      if (signature === nextSignature) return;
      const baseline = row.journal.find(entry => entry.type === 'evaluation' && entry.split === 'dev');
      if (!initial && baseline?.status === 'completed' && row.candidates.length) {
        const run = await data('/evals/runs/' + enc(baseline.id));
        const scored = run.passed + run.failed;
        if (scored) initial = {pass_rate: run.passed / scored, cost_usd: run.cost_usd};
      }
      const byId = new Map(row.candidates.map(candidate => [candidate.id, candidate.overlay]));
      if (canReadChampions && !parents && row.candidates.some(candidate => candidate.parent_id && !byId.has(candidate.parent_id))) {
        parents = new Map((await data(championsPath)).map(champion => [champion.candidate_id, champion.overlay]));
      }
      if (!active(state)) return;
      signature = nextSignature;
      const config = row.config;
      header.replaceChildren(el('h3', suiteLabel(config.suite) + ' · ' + targetLabel(config)),
        spend, el('small', id + ' · ' + ui.date(row.created_at)));
      if (row.reason) header.append(el('p', row.reason, 'bad portal-reason'));
      actions.replaceChildren(evidenceLink('返回演进记录', pageHref('runs')));
      if (row.best_champion_id && canReadChampions) {
        actions.append(detailLink('champions', row.best_champion_id, '查看晋升后的冠军'));
      }
      const groups = generations(row.candidates, initial, baseline?.id);
      plots.replaceChildren(...(groups.length ? [
        chart(groups, 'pass_rate', '开发集通过率', ui.percent),
        chart(groups, 'cost_usd', '开发集评测费用（美元）', ui.cost),
      ] : [el('p', '开发集评测后显示代际数据。')]));
      ui.list(baselineList, ['代数', '当代冠军 · 开发集通过率 / 费用', '已评测候选数'], groups.map(group => [
        '第 ' + group.number + ' 代', metricLine(group.champion, group.run),
        group.candidates.filter(item => item.dev_metrics).length,
      ]));
      const opened = new Set([...candidates.querySelectorAll('details[open]')].map(node => node.dataset.id));
      ui.list(candidates, ['代数', '假设', '决定与原因', '开发集', '留出集', '覆盖差异与操作'],
        row.candidates.map(candidate => {
          const reason = el('div', stateLabel(candidate.decision), 'stack');
          if (candidate.reason) reason.append(el('small', reasons[candidate.reason] || candidate.reason, 'portal-reason'));
          if (candidate.static_check?.reasons?.length) reason.append(el('small', candidate.static_check.reasons.join(' · ')));
          const detail = el('details');
          detail.dataset.id = candidate.id;
          const parent = candidate.parent_id
            ? byId.get(candidate.parent_id) ?? parents?.get(candidate.parent_id) ?? baseline?.overlay : {};
          detail.append(el('summary', '与父覆盖对比'),
            el('small', candidate.parent_id ? '父候选 · ' + short(candidate.parent_id) : '初始基线 · 空覆盖'));
          detail.ontoggle = () => {
            if (!detail.open || detail.childElementCount > 2) return;
            detail.append(parent ? overlayDiff(parent, candidate.overlay) : el('small', '父候选的覆盖内容不可用。'));
          };
          detail.open = opened.has(candidate.id);
          const operations = el('div', undefined, 'stack');
          operations.append(detail);
          if (pendingApproval(row, candidate)) {
            const link = evidenceLink('在对话中查看审批', pageHref('runs', id) + '/approval/' + enc(candidate.id));
            link.onclick = event => {
              event.preventDefault();
              portalApprovals.focus(approvalCards.get(candidate.id)?.node);
            };
            operations.append(link);
          }
          return [candidate.generation, candidate.hypothesis, reason,
            metricLine(candidate.dev_metrics, candidate.dev_run_id),
            metricLine(candidate.holdout_metrics, candidate.holdout_run_id), operations];
        }), '尚未提出候选。');
    };
  }

  function mountSchedules(state) {
    const path = schedulesPath, list = ui.scroll('定期演进计划'), edit = el('div'), message = notice();
    state.root.append(ui.panel('定期评测与演进', list, message), ui.panel('计划配置', edit));
    function editor(row = null) {
      const v = ui.value;
      const form = ui.form([
        el('h3', row ? '编辑计划 · ' + short(row.id) : '创建计划'),
        targetFields(row || {}), ui.fields(
          ui.field('执行间隔（小时）', 'interval_hours', {type: 'number', required: true, value: row?.interval_hours ?? 24, min: 0, step: 'any'}),
          ui.field('触发通过率阈值（%）', 'threshold', {type: 'number', required: true, value: (row?.threshold ?? .8) * 100, min: 0, max: 100, step: 'any'}),
          ui.field('晋升后自动灰度（%，留空停用）', 'auto_rollout_percent', {
            type: 'number', value: row?.auto_rollout_percent ?? '', min: 0, max: 100,
          }),
          ui.check('每次评测后均启动演进', 'always', row?.always === true),
          ui.check('启用计划', 'enabled', row?.enabled ?? true)
        ), configFields(row?.config),
      ], row ? '保存计划' : '创建计划', async form => {
        const body = {
          ...targetBody(form), split: 'dev', config: configBody(form),
          interval_hours: Number(v(form, 'interval_hours')), threshold: Number(v(form, 'threshold')) / 100,
          always: ui.checked(form, 'always'), enabled: ui.checked(form, 'enabled'),
          auto_rollout_percent: v(form, 'auto_rollout_percent') === '' ? null : Number(v(form, 'auto_rollout_percent')),
        };
        await data(row ? path + '/' + enc(row.id) : path, body, row ? 'PUT' : 'POST');
        ui.setMessage(message, row ? '计划已更新。' : '计划已创建。');
        editor();
        await refresh(state);
      });
      if (row) form.append(ui.actions(ui.action('取消编辑', () => editor())));
      evolutionBudget(form);
      edit.replaceChildren(form);
    }
    loadTargets(state, problem => {
      if (!edit.hasChildNodes()) editor();
      if (problem) ui.setMessage(message, problem.message, true);
    });
    return async () => {
      const rows = await data(path);
      ui.list(list, ['套件 / 后端 / 基线 / 模型', '设置', '最近结果', '操作'], rows.map(row => {
        const endpoint = path + '/' + enc(row.id);
        const latest = el('div', undefined, 'stack');
        latest.append(el('small', ui.date(row.last_run_at)));
        if (row.last_eval_run_id) latest.append(evidenceLink('开发集评测', runHref(row.last_eval_run_id)));
        if (row.last_evolution_id) latest.append(detailLink('runs', row.last_evolution_id, '查看演进'));
        return [
          row.suite + ' · ' + targetLabel(row),
          (row.enabled ? '启用' : '停用') + ' · 每 ' + row.interval_hours + ' 小时 · ' + ui.percent(row.threshold),
          latest,
          ui.actions(
            ui.action('编辑', async () => editor(await data(endpoint)), message),
            ui.action('立即运行', async () => {
              await data(endpoint + '/run-now', {});
              ui.setMessage(message, '已提交运行请求。');
              await refresh(state);
            }, message),
            ui.action('删除', async () => {
              if (!confirm('确认删除该计划？')) return;
              await api(endpoint, undefined, 'DELETE');
              editor();
              await refresh(state);
            }, message)
          ),
        ];
      }));
    };
  }

  function mountChampions(state) {
    const list = ui.scroll('冠军谱系');
    state.root.append(ui.panel('冠军谱系', list));
    return async () => {
      const rows = await data(championsPath);
      ui.list(list, ['冠军 ID', '后端 / 基线版本 / 模型', '状态', '父冠军', '开发集', '留出集', '创建时间'], rows.map(row => [
        detailLink('champions', row.id, row.id), targetLabel(row), row.active ? '当前冠军' : '历史冠军',
        detailLink('champions', row.parent_champion_id), metricLine(row.metrics?.dev), metricLine(row.metrics?.holdout),
        ui.date(row.created_at),
      ]), '尚无冠军。');
    };
  }

  function equivalence(a, b) {
    const key = row => JSON.stringify([row.case_id, row.repeat]);
    const left = new Map(a.results.map(row => [key(row), overallPassed(row)]));
    const right = new Map(b.results.map(row => [key(row), overallPassed(row)]));
    const complete = a.suite_hash === b.suite_hash && left.size > 0 && left.size === a.results.length
      && right.size === b.results.length && left.size === right.size && [...left.keys()].every(k => right.has(k));
    if (!complete) return '评测样本无法完整配对。';
    const matches = [...left].filter(([sample, passed]) => right.get(sample) === passed).length;
    return matches + ' / ' + left.size + ' 个样本验收判定一致 · '
      + ui.percent(a.passed / left.size) + ' → ' + ui.percent(b.passed / right.size)
      + ' · 费用 ' + ui.cost(a.cost_usd) + ' → ' + ui.cost(b.cost_usd);
  }

  function mountChampion(state) {
    const id = state.id, path = championsPath + '/' + enc(id), message = notice();
    const canManage = ui.can('eval-admin');
    const summary = el('div', undefined, 'stack'), builds = ui.scroll('固化构建');
    const form = canManage && ui.form([ui.field('构建备注（可选）', 'notes', {maxLength: 2000})], '固化冠军', async form => {
      await data(path + '/solidify', {notes: ui.value(form, 'notes') || null});
      form.reset();
      ui.setMessage(message, '已提交固化构建。');
      await refresh(state);
    });
    const buildPanel = ui.panel('固化构建与等价评测', builds);
    if (form) form.hidden = true;
    buildPanel.hidden = true;
    state.root.append(ui.panel('冠军详情', summary, ...(form ? [form] : []), message), buildPanel);
    let signature;
    const results = new Map();
    return async () => {
      const row = await data(path);
      if (!active(state)) return;
      const buildRows = row.solidify_builds || [];
      const nextSignature = JSON.stringify([row.active, buildRows]);
      state.polling = buildRows.some(build => ['running', 'queued'].includes(build.status) || build.regression_status === 'running'
        || (build.status === 'succeeded' && !build.regression_status));
      if (signature === nextSignature) return;
      signature = nextSignature;
      const runtime = row.agent_backend === 'dsh';
      if (form) form.hidden = runtime || !row.candidate_id;
      buildPanel.hidden = runtime;
      summary.replaceChildren(
        el('h3', targetLabel(row)),
        el('p', (row.active ? '当前冠军' : '历史冠军') + ' · ' + id, 'portal-wrap'),
        el('small', '开发集 · ' + metricLine(row.metrics?.dev) + ' | 留出集 · ' + metricLine(row.metrics?.holdout)),
        ui.actions(evidenceLink('返回冠军谱系', pageHref('champions')),
          row.parent_champion_id && detailLink('champions', row.parent_champion_id, '查看父冠军'),
          canManage && evidenceLink('设置灰度', pageHref('rollouts', row.id)),
          canManage && row.active && row.parent_champion_id && ui.action('回滚到父冠军', async () => {
            if (!confirm('确认回滚到父冠军？现有灰度比例会移交给父冠军。')) return;
            const parent = await data(path + '/rollback', {});
            if (active(state)) location.hash = pageHref('champions', parent.id);
          }, message))
      );
      ui.list(builds, ['版本 / 时间', '构建状态', '回归状态', '等价评测', '操作'], buildRows.map(build => {
        const before = build.overlay_eval_run_id, after = build.solidified_eval_run_id;
        const evidence = el('div', undefined, 'stack');
        const result = el('small', results.get(build.id) || '');
        evidence.append(ui.actions(before && evidenceLink('运行时覆盖', runHref(before)),
          after && evidenceLink('固化版本', runHref(after)),
          before && after && evidenceLink('逐用例对比', '#/compare/' + enc(before) + '/' + enc(after))), result);
        if (build.regression_status === 'completed' && before && after && !results.has(build.id)) {
          evidence.append(ui.actions(ui.action('查看配对结果', async () => {
            const runs = await Promise.all([before, after].map(id => data('/evals/runs/' + enc(id))));
            const text = equivalence(...runs);
            results.set(build.id, text);
            result.textContent = text;
          }, message)));
        }
        return [build.version + ' · ' + ui.date(build.created_at),
          stateLabel(build.status) + (build.error ? ' · ' + build.error : ''),
          build.regression_status ? stateLabel(build.regression_status) : '等待构建',
          evidence,
          canManage && build.status === 'succeeded' && build.regression_status === 'completed' ? ui.action('设为默认版本', async () => {
            if (!confirm('确认将 ' + build.version + ' 设为默认 OpenCode 版本？')) return;
            await data('/agents/opencode/versions/' + enc(build.version) + '/default', {});
            ui.setMessage(message, '平台默认版本已更新。');
          }, message) : '—',
        ];
      }), '尚无固化构建。');
    };
  }

  function mountRollouts(state) {
    const list = ui.scroll('冠军灰度配置'), edit = el('div'), message = notice();
    state.root.append(ui.panel('租户冠军灰度', list, message), ui.panel('设置灰度比例', edit));
    function editor(row = {}) {
      const v = ui.value;
      const form = ui.form([ui.fields(
        backendField(row.agent_backend),
        ui.field('基线版本', 'agent_version', {required: true, value: row.agent_version || ''}),
        portalModels.field('模型', 'model', {required: true, value: row.model || portalConfig?.default_model || ''}),
        ui.field('冠军 ID', 'champion_id', {required: true, value: row.champion_id || '',
          note: '填写 eval 评测空间提供的冠军 ID。'}),
        ui.field('灰度比例（%）', 'percent', {type: 'number', min: 0, max: 100, required: true,
          value: row.percent ?? 0, note: '0% 表示该版本和模型使用基线。'})
      )], '保存灰度', async form => {
        const body = Object.fromEntries(['agent_backend', 'agent_version', 'model', 'champion_id']
          .map(name => [name, v(form, name)]));
        await data(rolloutsPath, {...body, percent: Number(v(form, 'percent'))}, 'PUT');
        await refresh(state);
        return '灰度比例已保存。';
      });
      edit.replaceChildren(form);
    }
    editor();
    return async () => {
      const rows = await data(rolloutsPath);
      if (state.id && !state.seeded && ui.can('eval-admin')) {
        const champion = await data(championsPath + '/' + enc(state.id));
        if (!active(state)) return;
        const current = rows.find(row => (row.agent_backend || 'opencode') === (champion.agent_backend || 'opencode')
          && row.agent_version === champion.agent_version && row.model === champion.model);
        editor({...champion, champion_id: champion.id, percent: current?.percent});
        state.seeded = true;
      }
      ui.list(list, ['后端 / 基线版本 / 模型', '冠军', '灰度比例', '操作'], rows.map(row => [
        targetLabel(row), ui.can('eval-admin') ? detailLink('champions', row.champion_id) : row.champion_id,
        row.percent + '%', ui.action('编辑', () => editor(row)),
      ]), '尚无租户灰度配置。');
    };
  }

  function mountHarvest(state) {
    const rows = ui.scroll('采集候选'), message = notice();
    const target = el('div');
    target.hidden = true;
    const form = ui.form([ui.fields(
      ui.field('失败或被拒绝的任务 ID', 'task_id', {required: true, value: state.id || ''}),
      ui.field('目标套件', 'suite', {required: true, value: 'opencode-harvested'}),
      ui.field('评测分组', 'split', {choices: [['holdout', '留出集'], ['dev', '开发集']]}),
      ui.field('验收命令（可选）', 'acceptance_command', {note: '留空沿用任务验证命令。'}),
      ui.field('采集备注', 'notes', {multiline: true, wide: true, maxLength: 4000})
    )], '提交采集候选', async form => {
      const body = Object.fromEntries(['task_id', 'suite', 'split', 'notes'].map(name => [name, ui.value(form, name)]));
      const command = ui.value(form, 'acceptance_command');
      await data(harvestPath, {...body, ...(command && {acceptance_command: command})});
      void portalApprovals.refresh(true);
      form.reset();
      state.tenant = '';
      if (filter) filter.reset();
      await refresh(state);
      return '采集候选已提交。';
    });
    const filter = ui.can('eval-admin') ? ui.form([
      ui.field('来源租户（留空查看当前租户）', 'tenant_id', {placeholder: '租户 ID'}),
    ], '筛选', async form => {
      state.tenant = ui.value(form, 'tenant_id');
      await refresh(state);
    }) : null;
    state.tenant = '';
    state.root.append(ui.panel('采集失败任务', form),
      ui.panel('采集候选', ...(filter ? [filter] : []), target, rows, message));
    function operations(row, query, feedback) {
      if (row.status !== 'pending') return '—';
      const href = pageHref('harvest') + '/approval/' + enc(row.id);
      return ui.actions(
        ui.can('eval-admin') && ui.action('批准发布', async () => {
          if (!confirm('确认将该候选发布到目标评测套件？')) return;
          await portalApprovals.submit(harvestPath + '/' + enc(row.id) + '/approve' + query, {}, href);
          await refresh(state);
        }, feedback),
        ui.action('拒绝', async () => {
          await portalApprovals.submit(harvestPath + '/' + enc(row.id) + '/reject' + query, {}, href);
          await refresh(state);
        }, feedback),
      );
    }
    return async () => {
      const from = state.tenant;
      const query = from ? '?tenant_id=' + enc(from) : '';
      const list = await data(harvestPath + query);
      if (from !== state.tenant || !active(state)) return;
      target.hidden = !state.approval || Boolean(from);
      if (!target.hidden) {
        const row = list.find(item => item.id === state.approval);
        const card = portalApprovals.card('harvest-approval-' + state.approval, '采集发布审批');
        card.status.textContent = row ? row.status === 'pending' ? '等待发布审批' : stateLabel(row.status)
          : '该采集候选已不可用';
        if (row) {
          card.node.append(el('p', row.case_id + ' · ' + row.suite), el('p', row.notes),
            evidenceLink('查看来源任务', taskHref(row.task_id)));
          if (row.status === 'pending') card.node.append(operations(row, query, card.feedback), card.feedback);
        }
        target.replaceChildren(card.node);
        if (state.focusedApproval !== state.approval) {
          portalApprovals.focus(card.node);
          state.focusedApproval = state.approval;
        }
      }
      ui.list(rows, ['用例 / 套件', '分组', '来源任务', '备注', '状态', '操作'], list.map(row => [
        row.case_id + ' · ' + row.suite, row.split === 'dev' ? '开发集' : '留出集',
        row.tenant_id === state.identity.tenant_id ? evidenceLink(short(row.task_id), taskHref(row.task_id)) : row.task_id,
        row.notes, stateLabel(row.status), operations(row, query, message),
      ]), '当前租户无采集候选。');
    };
  }

  function refresh(state) {
    if (state.pending) return state.pending;
    state.pending = state.refresh().finally(() => {state.pending = null;});
    return state.pending;
  }

  function tabs() {
    if (!ui.can('evolution-history')) return [];
    const evalAdmin = ui.can('eval-admin');
    const visible = evalAdmin ? ['runs', 'schedules', 'champions', 'rollouts', 'harvest']
      : portalIdentity.guest ? ['runs', 'champions'] : ui.can('eval') ? ['runs'] : ['rollouts', 'harvest'];
    const titles = {runs: '演进记录', schedules: '定期计划', champions: '冠军谱系与固化', rollouts: '租户灰度', harvest: '采集候选'};
    return visible.map(key => [key, titles[key]]);
  }

  function currentTab(parts) {
    const visible = tabs().map(([key]) => key);
    return visible.includes(parts[0]) ? parts[0] : visible[0];
  }

  function load(parts) {
    const tab = currentTab(parts);
    const harvestApproval = parts[0] === 'harvest' && parts[1] === 'approval';
    const id = parts[0] === tab && parts[1] && !harvestApproval ? decodeURIComponent(parts[1]) : '';
    const approval = tab === 'harvest' && harvestApproval ? parts[2]
      : tab === 'runs' && parts[2] === 'approval' ? parts[3] : '';
    const root = $('evolution-content');
    if (view?.tab !== tab || view?.id !== id || view?.identity !== portalIdentity) {
      view?.dag?.destroy();
      view?.conversation?.destroy();
      const state = {root, tab, id, identity: portalIdentity, polling: tab === 'runs'};
      view = state;
      root.replaceChildren();
      const mount = {
        runs: id ? mountRun : mountRuns, schedules: mountSchedules, champions: id ? mountChampion : mountChampions,
        rollouts: mountRollouts, harvest: mountHarvest,
      }[tab];
      state.refresh = mount(state);
    }
    if (view.approval !== approval) {
      view.approval = approval;
      view.focusedApproval = '';
      if (tab === 'harvest') view.tenant = '';
    }
    return refresh(view);
  }

  function poll() {
    if (view?.polling) return refresh(view);
  }

  function reset() {
    if (!view) return;
    view.dag?.destroy();
    view.conversation?.destroy();
    view.root.replaceChildren();
    view = null;
  }

  return {load, poll, reset, lineChanges, generations, equivalence, tab: currentTab, tabs, suiteLabel,
    isActive: state => activeStates.includes(state)};
})();
