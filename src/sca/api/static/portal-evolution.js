'use strict';

const portalEvolution = (() => {
  const ui = portalUI;
  const runsPath = '/evolutions', schedulesPath = '/evolution-schedules';
  const championsPath = '/champions', rolloutsPath = '/champions/rollouts', harvestPath = '/evals/harvest';
  const taskHref = id => '#/tasks/' + enc(id);
  const runHref = id => '#/runs/' + enc(id);
  const pageHref = (tab, id) => '#/evolutions/' + tab + (id ? '/' + enc(id) : '');
  const short = value => String(value || '').slice(0, 8);
  const activeStates = ['queued', 'running', 'awaiting_approval'];
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
    awaiting_approval: '待审批', stopped: '已停止', patience_exhausted: '达到停滞上限',
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
    return name?.split('/').at(-1) || '—';
  }

  function configFields(config = {}) {
    const nodes = optionFields.map(([name, label, fallback, min, max, step]) => ui.field(label, name, {
      type: 'number', value: config[name] ?? fallback, min, ...(max !== null && {max}),
      step: step || 1, required: true,
    }));
    const toggles = surfaces.map(([name, label]) => ui.check(label, 'surface_' + name,
      !config.surfaces || config.surfaces.includes(name)));
    const details = el('details', undefined, 'portal-disclosure');
    details.append(el('summary', '候选、预算与晋升条件'), ui.fields(...nodes),
      ui.actions(...toggles), ui.check('晋升前需要人工审批', 'require_approval', config.require_approval === true));
    return details;
  }

  function configBody(form) {
    const config = Object.fromEntries(optionFields.map(([name]) => [name, Number(ui.value(form, name))]));
    config.require_approval = ui.checked(form, 'require_approval');
    config.surfaces = surfaces.map(([name]) => name).filter(name => ui.checked(form, 'surface_' + name));
    return config;
  }

  function targetFields(row = {}) {
    const qualified = suites.filter(suite => {
      const splits = new Set(Object.values(suite.case_metadata || {}).map(meta => meta.split));
      return splits.has('dev') && splits.has('holdout');
    });
    const names = new Set(['opencode-evolve', ...qualified.map(suite => suite.name)]);
    if (row.suite) names.add(row.suite);
    const result = ui.fields(
      ui.field('评测套件', 'suite', {required: true, value: row.suite || 'opencode-evolve',
        choices: [...names].map(name => [name, name])}),
      ui.field('OpenCode 基线版本', 'agent_version', {required: true,
        value: row.agent_version || versions[0]?.version || '', placeholder: '支持运行时覆盖的版本'}),
      ui.field('评测模型', 'model', {required: true, value: row.model || 'deepseek-flash'})
    );
    result.querySelector('[name="model"]').setAttribute('list', 'models');
    return result;
  }

  function targetBody(form) {
    return Object.fromEntries(['suite', 'agent_version', 'model'].map(name => [name, ui.value(form, name)]));
  }

  function mountRuns(state) {
    const list = ui.scroll('演进记录');
    const form = ui.form([targetFields(), configFields()], '开始演进', async form => {
      const result = await data(runsPath, {...targetBody(form), ...configBody(form)});
      if (active(state)) location.hash = pageHref('runs', result.id);
    });
    state.root.append(ui.panel('启动自动演进', form), ui.panel('演进记录', list));
    let signature;
    return async () => {
      const rows = await data(runsPath);
      const next = JSON.stringify(rows);
      if (signature === next) return;
      signature = next;
      ui.list(list, ['套件 / 演进 ID', '基线版本 / 模型', '状态', '支出 / 预算', '创建时间'], rows.map(row => [
        detailLink('runs', row.id, suiteLabel(row.config.suite) + ' · ' + short(row.id)),
        row.config.agent_version + ' · ' + row.config.model,
        stateLabel(row.status), ui.cost(row.spent_usd) + ' / ' + ui.cost(row.config.budget_usd), ui.date(row.created_at),
      ]), '尚无自动演进。');
    };
  }

  function metricLine(metrics, runId) {
    if (!metrics) return '—';
    const text = [
      ui.percent(metrics.pass_rate), ui.cost(metrics.cost_usd),
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
    const header = el('div', undefined, 'stack');
    const actions = ui.actions(evidenceLink('返回演进记录', pageHref('runs')));
    const message = notice(), plots = el('div', undefined, 'portal-charts'), baselineList = ui.scroll('各代冠军');
    const candidates = ui.scroll('演进候选');
    candidates.id = 'evolution-candidates';
    const legend = el('div', undefined, 'portal-chart-note');
    legend.append(el('span', '当代冠军'), el('span', '候选', 'candidate'));
    state.root.append(ui.panel('演进详情', header, actions, message),
      ui.panel('每代开发集成绩', legend, plots, baselineList), ui.panel('候选与晋升决定', candidates));
    let signature, initial, parents;
    return async () => {
      const row = await data(path);
      if (!active(state)) return;
      const nextSignature = JSON.stringify([row.status, row.spent_usd, row.best_champion_id,
        row.candidates.map(c => [c.id, c.decision, c.reason, c.dev_run_id, c.holdout_run_id]),
        row.journal.filter(entry => entry.type === 'evaluation').map(entry => [entry.id, entry.status])]);
      state.polling = activeStates.includes(row.status);
      if (signature === nextSignature) return;
      const baseline = row.journal.find(entry => entry.type === 'evaluation' && entry.split === 'dev');
      if (!initial && baseline?.status === 'completed' && row.candidates.length) {
        const run = await data('/evals/runs/' + enc(baseline.id));
        if (run.result_count) initial = {pass_rate: run.passed / run.result_count, cost_usd: run.cost_usd};
      }
      const byId = new Map(row.candidates.map(candidate => [candidate.id, candidate.overlay]));
      if (!parents && row.candidates.some(candidate => candidate.parent_id && !byId.has(candidate.parent_id))) {
        parents = new Map((await data(championsPath)).map(champion => [champion.candidate_id, champion.overlay]));
      }
      if (!active(state)) return;
      signature = nextSignature;
      const config = row.config;
      header.replaceChildren(el('h3', suiteLabel(config.suite) + ' · ' + config.agent_version + ' · ' + config.model),
        el('p', stateLabel(row.status) + ' · 已用 ' + ui.cost(row.spent_usd) + ' / ' + ui.cost(config.budget_usd)),
        el('small', id + ' · ' + ui.date(row.created_at)));
      actions.replaceChildren(evidenceLink('返回演进记录', pageHref('runs')));
      if (state.polling) actions.append(ui.action('停止演进', async () => {
        if (!confirm('确认停止这次演进？')) return;
        await data(path + '/stop', {});
        ui.setMessage(message, '已发送停止请求。');
        await refresh(state);
      }, message));
      if (row.best_champion_id) actions.append(detailLink('champions', row.best_champion_id, '查看晋升后的冠军'));
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
          if (row.status === 'awaiting_approval' && candidate.decision === 'pending' && candidate.reason === 'awaiting approval') {
            operations.append(ui.actions(...[['approve', '批准'], ['reject', '拒绝']].map(([action, label]) =>
              ui.action(label, async () => {
                await data(path + '/' + action, {candidate_id: candidate.id});
                ui.setMessage(message, '已提交审批，等待演进继续。');
                await refresh(state);
              }, message))));
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
      edit.replaceChildren(form);
    }
    editor();
    return async () => {
      const rows = await data(path);
      ui.list(list, ['套件 / 基线 / 模型', '设置', '最近结果', '操作'], rows.map(row => {
        const endpoint = path + '/' + enc(row.id);
        const latest = el('div', undefined, 'stack');
        latest.append(el('small', ui.date(row.last_run_at)));
        if (row.last_eval_run_id) latest.append(evidenceLink('开发集评测', runHref(row.last_eval_run_id)));
        if (row.last_evolution_id) latest.append(detailLink('runs', row.last_evolution_id, '查看演进'));
        return [
          [row.suite, row.agent_version, row.model].join(' · '),
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
      ui.list(list, ['冠军 ID', '基线版本 / 模型', '状态', '父冠军', '开发集', '留出集', '创建时间'], rows.map(row => [
        detailLink('champions', row.id, row.id), row.agent_version + ' · ' + row.model, row.active ? '当前冠军' : '历史冠军',
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
    const summary = el('div', undefined, 'stack'), builds = ui.scroll('固化构建');
    const form = ui.form([ui.field('构建备注（可选）', 'notes', {maxLength: 2000})], '固化冠军', async form => {
      await data(path + '/solidify', {notes: ui.value(form, 'notes') || null});
      form.reset();
      ui.setMessage(message, '已提交固化构建。');
      await refresh(state);
    });
    state.root.append(ui.panel('冠军详情', summary, form, message), ui.panel('固化构建与等价评测', builds));
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
      form.hidden = !row.candidate_id;
      summary.replaceChildren(
        el('h3', row.agent_version + ' · ' + row.model),
        el('p', (row.active ? '当前冠军' : '历史冠军') + ' · ' + id, 'portal-wrap'),
        el('small', '开发集 · ' + metricLine(row.metrics?.dev) + ' | 留出集 · ' + metricLine(row.metrics?.holdout)),
        ui.actions(evidenceLink('返回冠军谱系', pageHref('champions')),
          row.parent_champion_id && detailLink('champions', row.parent_champion_id, '查看父冠军'),
          evidenceLink('设置灰度', pageHref('rollouts', row.id)),
          row.active && row.parent_champion_id && ui.action('回滚到父冠军', async () => {
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
          build.status === 'succeeded' && build.regression_status === 'completed' ? ui.action('设为默认版本', async () => {
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
        ui.field('OpenCode 基线版本', 'agent_version', {required: true, value: row.agent_version || ''}),
        ui.field('模型', 'model', {required: true, value: row.model || ''}),
        ui.field('冠军 ID', 'champion_id', {required: true, value: row.champion_id || '',
          note: '填写 eval 评测空间提供的冠军 ID。'}),
        ui.field('灰度比例（%）', 'percent', {type: 'number', min: 0, max: 100, required: true,
          value: row.percent ?? 0, note: '0% 表示该版本和模型使用基线。'})
      )], '保存灰度', async form => {
        const body = Object.fromEntries(['agent_version', 'model', 'champion_id'].map(name => [name, v(form, name)]));
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
        const current = rows.find(row => row.agent_version === champion.agent_version && row.model === champion.model);
        editor({...champion, champion_id: champion.id, percent: current?.percent});
        state.seeded = true;
      }
      ui.list(list, ['基线版本', '模型', '冠军', '灰度比例', '操作'], rows.map(row => [
        row.agent_version, row.model, ui.can('eval-admin') ? detailLink('champions', row.champion_id) : row.champion_id,
        row.percent + '%', ui.action('编辑', () => editor(row)),
      ]), '尚无租户灰度配置。');
    };
  }

  function mountHarvest(state) {
    const rows = ui.scroll('采集候选'), message = notice();
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
      ui.panel('采集候选', ...(filter ? [filter] : []), rows, message));
    return async () => {
      const from = state.tenant;
      const query = from ? '?tenant_id=' + enc(from) : '';
      const list = await data(harvestPath + query);
      if (from !== state.tenant) return;
      ui.list(rows, ['用例 / 套件', '分组', '来源任务', '备注', '状态', '操作'], list.map(row => [
        row.case_id + ' · ' + row.suite, row.split === 'dev' ? '开发集' : '留出集',
        row.tenant_id === state.identity.tenant_id ? evidenceLink(short(row.task_id), taskHref(row.task_id)) : row.task_id,
        row.notes, stateLabel(row.status), row.status === 'pending' ? ui.actions(
          ui.can('eval-admin') && ui.action('批准发布', async () => {
            if (!confirm('确认将该候选发布到目标评测套件？')) return;
            await data(harvestPath + '/' + enc(row.id) + '/approve' + query, {});
            await refresh(state);
          }, message),
          ui.action('拒绝', async () => {
            await data(harvestPath + '/' + enc(row.id) + '/reject' + query, {});
            await refresh(state);
          }, message)
        ) : '—',
      ]), '当前租户无采集候选。');
    };
  }

  function refresh(state) {
    if (state.pending) return state.pending;
    state.pending = state.refresh().finally(() => {state.pending = null;});
    return state.pending;
  }

  function load(parts) {
    const evalAdmin = ui.can('eval-admin');
    const visible = evalAdmin ? ['runs', 'schedules', 'champions', 'rollouts', 'harvest'] : ['rollouts', 'harvest'];
    const tab = visible.includes(parts[0]) ? parts[0] : visible[0], id = parts[1] ? decodeURIComponent(parts[1]) : '';
    const root = $('evolution-content');
    const titles = {runs: '演进记录', schedules: '定期计划', champions: '冠军谱系与固化', rollouts: '租户灰度', harvest: '采集候选'};
    $('evolution-nav').replaceChildren(...visible.map(key => {
      const link = evidenceLink(titles[key], pageHref(key));
      link.className = 'button';
      if (key === tab) link.setAttribute('aria-current', 'page');
      return link;
    }));
    if (view?.tab !== tab || view?.id !== id || view?.identity !== portalIdentity) {
      const state = {root, tab, id, identity: portalIdentity, polling: tab === 'runs'};
      view = state;
      root.replaceChildren();
      const mount = {
        runs: id ? mountRun : mountRuns, schedules: mountSchedules, champions: id ? mountChampion : mountChampions,
        rollouts: mountRollouts, harvest: mountHarvest,
      }[tab];
      state.refresh = mount(state);
    }
    return refresh(view);
  }

  function poll() {
    if (view?.polling) return refresh(view);
  }

  function reset() {
    view = null;
    $('evolution-content').replaceChildren();
  }

  return {load, poll, reset, lineChanges, generations, equivalence};
})();
