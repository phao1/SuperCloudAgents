'use strict';

window.SCAPortalArchive = (() => {
  const {panel} = portalUI;
  const sourceRoot = 'https://github.com/phao1/SuperCloudAgents/blob/supercloudagent1122/Milestone/';
  const orderSource = 'portal-run-2026-11-19.md';
  const definitions = {
    projects: ['projects-page', '项目', '从已公开的场景进入执行记录。'],
    home: ['home-page', '首页', '已归档的任务与评测结果。'],
    submit: ['submit-page', '提交任务示例', '查看订单任务使用的需求与验收条件。'],
    evals: ['evaluation', '评测', '查看已发布的评测运行和用例结果。'],
    runs: ['run-detail', '评测运行详情', '浏览已有结果与公开证据。'],
    tasks: ['trace', '任务', '沿架构图和执行泳道回放已归档的任务。'],
    history: ['archive-history-page', '任务历史', '浏览和筛选完整的公开任务列表。'],
    analysis: ['analysis-page', '数据分析', '查看已归档的数据分析任务。'],
    compare: ['compare-page', '评测', '查看同一评测套件在修复前后的结果。'],
    versions: ['versions', '版本', '查看公开记录里的 OpenCode 版本。'],
    loop: ['loop', '自动演进', '回看文件工具的评测、修复和回归记录。'],
    evolutions: ['evolutions-page', '自动演进', '查看已发布的迭代证据。'],
    memories: ['memories-page', '共享记忆', '浏览已公开的约定，查看任务何时命中记忆。'],
    tools: ['tools-page', '工具与知识库', '查看归档里的工具调用和参考资料。'],
    integrations: ['integrations-page', '集成', '查看已有的 CI 调用与结果。'],
    team: ['team-page', '团队与用量', '按 Agent 后端查看归档任务量。'],
    platform: ['platform-page', '平台管理', '查看本地归档信息与部署说明。'],
  };
  let archive;

  function metrics(...children) {
    const node = el('div', undefined, 'metrics');
    node.append(...children);
    return node;
  }

  function sourceLink(path) {
    const link = evidenceLink('查看原始记录 ↗', sourceRoot + path);
    link.target = '_blank';
    link.rel = 'noopener';
    return link;
  }

  function runLink(run) {
    return evidenceLink(run.title, '#/runs/' + enc(run.id));
  }

  function taskLink(id, label = id, event) {
    return evidenceLink(label, '#/tasks/' + enc(id) + (event ? '/events/' + event : ''));
  }

  function runById(id) {
    return archive.runs.find(run => run.id === id);
  }

  function rate(run) {
    return Math.round(100 * run.passed / run.total) + '%';
  }

  function resultsTable() {
    return table(['评测运行', '日期', '套件', '版本', '通过率'], archive.runs.map(run => [
      runLink(run), run.date, run.suite, run.version, run.passed + '/' + run.total + ' · ' + rate(run),
    ]));
  }

  function overview() {
    const succeeded = archive.tasks.filter(task => task.state === 'SUCCEEDED').length;
    const events = archive.tasks.reduce((sum, task) => sum + task.eventCount, 0);
    const links = el('div', undefined, 'result-links');
    links.append(evidenceLink('查看任务进度 →', '#/tasks'), evidenceLink('查看任务历史 →', '#/history'),
      evidenceLink('查看共享记忆 →', '#/memories'), evidenceLink('查看架构图 →', '/portal/architecture'));
    $('home-page').replaceChildren(
      metrics(
        metric('已归档任务', archive.tasks.length, succeeded + ' 条成功'),
        metric('执行事件', events.toLocaleString('zh-CN'), '点击任务可查看时间线'),
        metric('评测运行', archive.runs.length, '公开的基线和回归记录'),
        metric('OpenCode 版本', archive.versions.length, '见版本谱系'),
      ),
      panel('评测历史', resultsTable()),
      panel('任务回放', links),
    );
  }

  function evaluations() {
    $('evaluation').replaceChildren(panel('已归档评测', resultsTable()));
  }

  function runDetail(id) {
    const run = runById(id);
    const back = evidenceLink('返回评测运行', '#/evals');
    if (!run) {
      $('run-detail').replaceChildren(panel('未找到这条评测记录', back));
      return;
    }
    const section = panel(run.title, el('p', [run.date, run.suite, run.version, run.model].join(' · ')),
      metrics(metric('总体通过率', rate(run), run.passed + ' / ' + run.total + ' 项通过')));
    if (run.note) section.append(el('p', run.note));
    const linkRow = el('div', undefined, 'result-links');
    linkRow.append(back, sourceLink(run.source));
    section.append(linkRow);
    $('run-detail').replaceChildren(section);
    if (run.cases?.length) {
      const result = value => el('span', value ? '通过' : '失败', value ? 'good' : 'bad');
      $('run-detail').append(panel('用例结果', table(['用例', '次数', '验证', '过程检查', '独立验收', '耗时'],
        run.cases.map(entry => [
          entry.name, entry.repeat, result(entry.verification), result(entry.trajectory),
          result(entry.acceptance), entry.duration.toFixed(2) + ' 秒',
        ]))));
    }
  }

  async function tasks(id, detail, seq) {
    const hash = location.hash;
    if (!serverTasks.length) await loadRecentTasks();
    if (hash !== location.hash || !archive.tasks.length) return;
    if (!id) {
      id = enc(archive.tasks[0].id);
      history.replaceState(null, '', '#/tasks/' + id);
    }
    const taskId = decodeURIComponent(id);
    if (!archive.tasks.some(task => task.id === taskId)) {
      stopReplay();
      $('task-details').hidden = true;
      throw Error('归档中没有任务 ' + taskId + '。');
    }
    const destination = location.hash;
    await loadTaskRoute([id, detail, seq]);
    if (destination === location.hash && detail === 'events') {
      const index = visibleSteps.findIndex(step => step.seq === Number(seq));
      if (index >= 0) setReplayPosition(index);
    }
  }

  function taskHistory() {
    if ($('archive-history-frame')) return;
    const frame = el('iframe');
    frame.id = 'archive-history-frame';
    frame.src = '/portal/history';
    frame.title = '任务历史与执行时间线';
    frame.style.cssText = 'display:block;width:100%;height:max(660px,calc(100vh - 190px));border:0';
    $('archive-history-page').append(frame);
  }

  function analysis() {
    const completed = archive.tasks.filter(task => task.kind === 'analysis');
    const note = el('p', '任务状态与耗时来自公开归档，报告正文需在原工作空间查看。');
    const contents = completed.length
      ? table(['任务', '状态', '日期', '耗时', '事件'], completed.map(task => [
          taskLink(task.id), status(task.state), task.date, (task.elapsedMs / 1000).toFixed(1) + ' 秒', task.eventCount,
        ]))
      : el('p', '归档中没有数据分析任务。', 'empty-state');
    $('analysis-page').replaceChildren(panel('分析任务', contents, note, evidenceLink('查看任务历史 →', '#/history')));
  }

  function compare() {
    const {baseline, candidate, fixed, regressions, source} = archive.comparison;
    const original = runById(baseline);
    const revised = runById(candidate);
    const scores = metrics(
      metric('总体通过率', rate(original) + ' → ' + rate(revised), original.suite),
      metric('已修复用例', fixed, '基线失败、回归通过'),
      metric('回归用例', regressions, '基线通过、回归失败'),
    );
    const versions = table(['阶段', '评测运行', '版本', '通过数'], [
      ['基线', runLink(original), original.version, original.passed + '/' + original.total],
      ['回归', runLink(revised), revised.version, revised.passed + '/' + revised.total],
    ]);
    $('compare-page').replaceChildren(panel('文件工具修复前后', scores, versions, sourceLink(source)));
  }

  function versions() {
    $('versions').replaceChildren(panel('OpenCode 版本', table(['版本', '基线版本', '说明', '记录'],
      archive.versions.map(version => [
        version.version, version.parent || '—', version.label, sourceLink(version.source),
      ]))));
  }

  function loop() {
    const {baseline, candidate, source} = archive.comparison;
    const before = runById(baseline);
    const after = runById(candidate);
    $('loop').replaceChildren(panel('文件工具修复演练', table(['步骤', '已归档结果'], [
      ['评测', runLink(before)],
      ['定位', '文件编辑证据显示中文内容被清空。'],
      ['修复', '调整 OpenCode 文件写入逻辑，并补充中文回归用例。'],
      ['构建', evidenceLink(after.version + ' · 版本谱系', '#/versions')],
      ['回归', runLink(after)],
    ]), evidenceLink('查看版本对比 →', '#/compare'), sourceLink(source)));
  }

  function projects() {
    $('projects-page').replaceChildren(panel('已发布的项目场景', table(['场景', '记录'], [
      ['订单流水校验、金额汇总与 CSV 导出', taskLink('T008', '完整重跑 T008')],
      ['订单任务沙箱恢复复验', taskLink('T010', '共享记忆与 CI 复验 T010')],
      ['OpenCode 文件工具修复', evidenceLink('文件工具修复演练', '#/loop')],
      ['OpenCode 系统提示词迭代', runLink(runById('prompt-baseline'))],
    ]), evidenceLink('查看任务提交示例', '#/submit'), sourceLink(orderSource)));
  }

  function submissionExample() {
    $('submit-page').replaceChildren(panel('订单任务需求与验收',
      el('p', '校验订单字段，按币种汇总整数分金额，并导出 CSV。执行记录包含 Ruff、单元测试、CI 和独立验收。'),
      taskLink('T008', '查看这项任务的执行过程 →'), sourceLink(orderSource + '#订单任务规格')));
  }

  function evolutions() {
    const {baseline, candidate, source} = archive.comparison;
    $('evolutions-page').replaceChildren(panel('公开的迭代结果', table(['阶段', '评测记录'], [
      ['系统提示词基线', runLink(runById('prompt-baseline'))],
      ['文件工具基线', runLink(runById(baseline))],
      ['文件工具回归', runLink(runById(candidate))],
    ]), evidenceLink('查看自演进闭环 →', '#/loop'), sourceLink(source),
    el('p', '晋升记录、定期计划和灰度配置需连接有权限的在线工作空间。')));
  }

  async function memories() {
    if (!$('archive-memory-context')) {
      const context = panel('共享记忆归档', metrics(
        metric('已公开的记忆正文', archive.memories.length, '订单演示仓库约定'),
        metric('历史查询', archive.eventCounts['memory.injected'] ?? 0, archive.memoryHits + ' 次命中'),
      ), sourceLink(orderSource + '#L108'));
      context.id = 'archive-memory-context';
      $('memories-page').prepend(context);
    }
    await loadMemories();
  }

  function toolRecords() {
    $('tools-content').replaceChildren(panel('归档中的工具事件', table(['事件', '次数'], [
      ['工具调用', archive.eventCounts['agent.tool'] ?? 0],
      ['命令执行', archive.eventCounts['agent.command'] ?? 0],
    ]), taskLink('T008', '在任务进度中查看调用 →'), sourceLink(orderSource + '#查看流程和模块说明'),
    el('p', '工作空间的 MCP 工具配置和知识库文档可在有权限的在线工作空间查看。')));
  }

  function integrations() {
    $('integrations-content').replaceChildren(panel('CI 执行记录',
      metrics(metric('CI 请求', archive.eventCounts['ci.requested'] ?? 0),
        metric('CI 完成', archive.eventCounts['ci.completed'] ?? 0)),
      table(['订单任务 T010', '结果'], [
        ['第 1 轮', taskLink('T010', '失败 · 事件 #69', 69)],
        ['第 2 轮', taskLink('T010', '通过 · 事件 #88', 88)],
      ]), sourceLink(orderSource + '#全屏回放和沙箱恢复复验')));
  }

  function team() {
    const counts = new Map();
    for (const {backend} of archive.tasks) counts.set(backend, (counts.get(backend) || 0) + 1);
    const rows = [...counts].sort((a, b) => b[1] - a[1]).map(([backend, count]) => [
      portalUI.backendLabel(backend) || '未记录后端', count,
    ]);
    $('team-content').replaceChildren(panel('归档任务量', table(['Agent 后端', '任务数'], rows),
      el('p', '团队成员、配额、Token 与费用需在有权限的在线工作空间查看。')));
  }

  function platform() {
    $('platform-content').replaceChildren(panel('归档工作空间', table(['项目', '当前值'], [
      ['访问方式', '本机只读'], ['任务数', archive.tasks.length], ['公开评测数', archive.runs.length],
    ]), sourceLink(orderSource + '#本机部署和配置'),
    el('p', '平台租户、套餐和配额由在线实例的管理员查看。')));
  }

  const renderers = {projects, home: overview, submit: submissionExample, evals: evaluations, runs: runDetail,
    tasks, history: taskHistory, analysis, compare, versions, loop, evolutions, memories,
    tools: toolRecords, integrations, team, platform};

  async function route() {
    if (!archive) return;
    const hash = location.hash;
    const {page, parts} = portalNav.resolve(hash);
    const routeName = Object.hasOwn(definitions, page) ? page : 'home';
    if (routeName !== 'tasks') {
      stopReplay();
      if (traceFullscreen()) await setTraceFullscreen(false);
    }
    if (hash !== location.hash) return;
    $('evidence').hidden = true;
    evidenceView = null;
    $('error').textContent = '';
    portalNav.display(routeName, parts, definitions);
    await renderers[routeName](...parts);
  }

  async function loadArchive() {
    const button = $('refresh');
    button.disabled = true;
    try {
      const response = await fetch('/portal/archive.json');
      if (!response.ok) throw Error('无法读取仓库里的历史归档（' + response.status + '）。');
      archive = await response.json();
      await route();
    } finally {
      button.disabled = false;
    }
  }

  async function start() {
    const banner = $('guest-view-banner');
    banner.textContent = '只读归档 · 来自仓库中已发布的任务与评测记录';
    banner.hidden = false;
    $('workspace-indicator').className = 'badge complete';
    $('workspace-indicator').textContent = '本地归档';
    $('sidebar-workspace').textContent = '归档工作空间';
    portalNav.sync(definitions);
    const historyPage = el('section', undefined, 'page');
    historyPage.id = 'archive-history-page';
    historyPage.hidden = true;
    $('trace').after(historyPage);
    portalIdentity = {tenant_id: 'eval', role: 'viewer', guest: true};
    $('trace-live').textContent = '回到最新事件';
    $('home-page').replaceChildren(el('p', '正在读取归档…', 'empty-state'));
    $('refresh').onclick = () => loadArchive().catch(error);
    window.addEventListener('hashchange', () => route().catch(error));
    await loadArchive();
  }

  return {start};
})();
