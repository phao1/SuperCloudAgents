'use strict';

window.SCAPortalArchive = (() => {
  const sourceRoot = 'https://github.com/phao1/SuperCloudAgents/blob/supercloudagent1122/Milestone/';
  const definitions = {
    overview: ['overview', '概览', '已归档的任务与评测结果。'],
    evals: ['evaluation', '评测中心', '查看已发布的评测运行和用例结果。'],
    runs: ['run-detail', '评测运行详情', '浏览已有结果与公开证据。'],
    tasks: ['trace', '任务历史', '选择任务，展开执行时间线。'],
    analysis: ['analysis-page', '数据分析', '查看已归档的数据分析任务。'],
    compare: ['compare-page', '版本对比', '查看同一评测套件在修复前后的结果。'],
    versions: ['versions', '版本谱系', '查看公开记录里的 OpenCode 版本。'],
    loop: ['loop', '自演进闭环', '回看文件工具的评测、修复和回归记录。'],
  };
  let archive;

  function panel(title, ...children) {
    const node = el('div', undefined, 'panel');
    node.append(el('h2', title), ...children);
    return node;
  }

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
    links.append(evidenceLink('查看任务历史 →', '#/tasks'), evidenceLink('查看架构图 →', '/portal/architecture'));
    $('overview').replaceChildren(
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
    const back = evidenceLink('返回评测中心', '#/evals');
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

  function tasks() {
    if ($('archive-history-frame')) return;
    const frame = el('iframe');
    frame.id = 'archive-history-frame';
    frame.src = '/portal/history';
    frame.title = '任务历史与执行时间线';
    frame.style.cssText = 'display:block;width:100%;height:max(660px,calc(100vh - 190px));border:0';
    $('trace').replaceChildren(frame);
  }

  function analysis() {
    const completed = archive.tasks.filter(task => task.kind === 'analysis');
    const note = el('p', '任务状态与耗时来自公开归档，报告正文需在原工作空间查看。');
    const contents = completed.length
      ? table(['任务', '状态', '日期', '耗时', '事件'], completed.map(task => [
          task.id, status(task.state), task.date, (task.elapsedMs / 1000).toFixed(1) + ' 秒', task.eventCount,
        ]))
      : el('p', '归档中没有数据分析任务。', 'empty-state');
    $('analysis-page').replaceChildren(panel('分析任务', contents, note, evidenceLink('查看任务历史 →', '#/tasks')));
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

  const renderers = {overview, evals: evaluations, runs: runDetail, tasks, analysis, compare, versions, loop};

  function route() {
    if (!archive) return;
    const [requested, part] = location.hash.slice(2).split('/');
    const routeName = Object.hasOwn(definitions, requested) ? requested : 'overview';
    const [section, title, description] = definitions[routeName];
    $('page-title').textContent = title;
    $('page-description').textContent = description;
    for (const node of document.querySelectorAll('main > section')) node.hidden = node.id !== section;
    for (const link of document.querySelectorAll('.sidebar nav a')) {
      if (link.hash === '#/' + (routeName === 'runs' ? 'evals' : routeName)) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    }
    renderers[routeName](part);
  }

  async function loadArchive() {
    const button = $('refresh');
    button.disabled = true;
    try {
      const response = await fetch('/portal/archive.json');
      if (!response.ok) throw Error('无法读取仓库里的历史归档（' + response.status + '）。');
      archive = await response.json();
      $('error').textContent = '';
      route();
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
    document.querySelector('.sidebar .brand').href = '#/overview';
    for (const link of document.querySelectorAll('.sidebar nav a')) {
      const page = link.hash.slice(2);
      link.hidden = !Object.hasOwn(definitions, page);
      if (page === 'tasks') link.textContent = definitions.tasks[1];
    }
    $('overview').replaceChildren(el('p', '正在读取归档…', 'empty-state'));
    $('refresh').onclick = () => loadArchive().catch(error);
    window.addEventListener('hashchange', route);
    await loadArchive();
  }

  return {start};
})();
