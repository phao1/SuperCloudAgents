'use strict';

const portalNav = (() => {
  const settings = {
    models: ['models-page', '模型', '管理模型供应商，并选择默认模型。', 'tenant'],
    runtime: ['runtime-page', '运行环境', '查看当前 Portal 的运行模式。', 'tenant'],
    access: ['access-page', '访问与安全', '连接工作空间并管理本机访问。', 'public'],
    integrations: ['integrations-page', '集成', '管理代码托管集成并查看设计与工单的凭证配置。', 'admin'],
    team: ['team-page', '团队与用量', '管理成员邀请、API 令牌与租户用量。', 'admin'],
    platform: ['platform-page', '平台管理', '管理租户和套餐，查看平台用量。', 'platform'],
  };
  const pages = {
    home: ['home-page', '首页', '查看工作空间的任务、演进和模型状态。', 'public'],
    projects: ['projects-page', '项目', '按项目管理任务、文件和用量。', 'tenant'],
    tasks: ['trace', '任务', '浏览任务，查看对话、执行流程与证据。', 'history'],
    submit: ['submit-page', '新建任务', '配置仓库、验证命令和任务所需的上下文。', 'member'],
    approvals: ['approvals-page', '待审批', '查看当前工作空间等待人工处理的事项。', 'tenant'],
    evals: ['evaluation', '评测', '配置评测条件，查看每个用例的结果。', 'eval-history'],
    runs: ['run-detail', '评测运行详情', '从结果矩阵打开任务证据。', 'eval-history'],
    compare: ['compare-page', '评测', '比较运行的通过率、耗时和费用。', 'eval-history'],
    evolutions: ['evolutions-page', '自动演进', '查看演进执行图、开发集与留出集证据。', 'evolution-history'],
    loop: ['loop', '自动演进', '追踪从基线、候选到回归的进度。', 'eval'],
    examples: ['evolution-examples', '自动演进', '从示例评测开始一次自演进。', 'eval'],
    versions: ['versions', '版本', '按源码提交和构建记录追溯 OpenCode 版本。', 'tenant'],
    memories: ['memories-page', '共享记忆', '管理跨任务复用的约定。', 'tenant'],
    tools: ['tools-page', '工具与知识库', '查看 MCP 工具和知识库文档。', 'tenant'],
    analysis: ['analysis-page', '数据分析', '上传数据并追踪分析报告。', 'history'],
    ...settings,
  };
  const families = {runs: 'evals', compare: 'evals', submit: 'tasks', history: 'tasks',
    loop: 'evolutions', examples: 'evolutions'};

  function resolve(hash) {
    const [name, ...parts] = hash.startsWith('#/') ? hash.slice(2).split('/') : [];
    if (name === 'settings') {
      const [tab, ...rest] = parts;
      return {page: Object.hasOwn(settings, tab) ? tab : 'access', parts: rest};
    }
    if ((name === 'evals' && parts[0] === 'compare')
        || (name === 'evolutions' && ['loop', 'examples'].includes(parts[0]))) {
      return {page: parts[0], parts: parts.slice(1)};
    }
    return {page: name === 'overview' ? 'home' : name, parts};
  }

  function liveRoute(hash) {
    const {page: requested, parts} = resolve(hash);
    if (portalIdentity?.guest && (!hash || requested === 'projects')) {
      return {page: 'tasks', current: 'tasks', parts: [], redirect: '#/tasks'};
    }
    const page = Object.hasOwn(pages, requested) ? requested : 'home';
    return {page, parts, current: portalUI.can(pages[page][3]) ? page : 'home'};
  }

  function sync(archivePages) {
    $('portal-brand').href = !archivePages && portalIdentity?.guest ? '#/tasks' : '#/home';
    for (const link of $('primary-nav').querySelectorAll('a')) {
      const key = link.dataset.nav;
      if (archivePages) {
        link.hidden = !Object.hasOwn(archivePages, key);
      } else if (key === 'evolutions') {
        const runs = portalUI.can('evolution-history');
        link.hidden = !runs && !portalUI.can('eval');
        link.href = runs ? '#/evolutions' : '#/evolutions/loop';
      } else if (key === 'settings') {
        link.hidden = portalIdentity?.guest === true;
      }
    }
  }

  function tabs(page, parts, family, archive) {
    if (family === 'settings' && !archive) {
      return ['设置导航', page, Object.entries(settings)
        .filter(([, [, , , access]]) => portalUI.can(access))
        .map(([key, [, title]]) => [key, title, '#/settings/' + key])];
    }
    if (family === 'evals') {
      return ['评测导航', page === 'compare' ? 'compare' : 'evals', [
        ['evals', '评测运行', '#/evals'], ['compare', '版本对比', '#/evals/compare'],
      ]];
    }
    if (family === 'evolutions') {
      const runTabs = archive ? [['runs', '演进记录']] : portalEvolution.tabs();
      const list = runTabs.map(([key, title]) => [key, title, '#/evolutions/' + key]);
      if (archive || portalUI.can('eval')) list.push(['loop', '自演进闭环', '#/evolutions/loop']);
      if (!archive && portalUI.can('eval')) list.push(['examples', '示例', '#/evolutions/examples']);
      const active = page === 'evolutions' ? (archive ? 'runs' : portalEvolution.tab(parts)) : page;
      return ['自动演进导航', active, list];
    }
    return ['', '', []];
  }

  function display(page, parts, definitions = pages) {
    const archive = definitions !== pages;
    const [target, title, description] = definitions[page];
    const inSettings = Object.hasOwn(settings, page);
    const family = inSettings ? 'settings' : families[page] || page;
    $('page-title').textContent = inSettings ? '设置 · ' + title : title;
    $('page-description').textContent = description;
    for (const node of document.querySelectorAll('main > section')) {
      node.hidden = node.id !== (inSettings ? 'settings-page' : target);
    }
    for (const node of $('settings-page').querySelectorAll('[data-settings]')) node.hidden = node.id !== target;
    for (const link of $('primary-nav').querySelectorAll('a')) {
      const active = page === 'history' ? 'history' : family;
      if (link.dataset.nav === active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    const [label, active, links] = tabs(page, parts, family, archive);
    const nav = $('page-tabs');
    const focused = nav.contains(document.activeElement);
    nav.hidden = !links.length;
    nav.setAttribute('aria-label', label);
    nav.replaceChildren(...links.map(([key, name, href]) => {
      const link = evidenceLink(name, href);
      if (key === active) link.setAttribute('aria-current', 'page');
      return link;
    }));
    if (focused) (nav.querySelector('[aria-current="page"]') || $('page-title')).focus();
  }

  function bindMenu() {
    const toggle = $('nav-toggle'), drawer = $('nav-drawer');
    function setOpen(open) {
      toggle.setAttribute('aria-expanded', String(open));
      drawer.toggleAttribute('data-open', open);
      toggle.setAttribute('aria-label', open ? '关闭主导航' : '打开主导航');
      $('nav-toggle-icon').setAttribute('href', open ? '#icon-close' : '#icon-menu');
    }
    toggle.onclick = () => setOpen(!drawer.hasAttribute('data-open'));
    drawer.addEventListener('click', event => {
      if (event.target.closest('a') && drawer.hasAttribute('data-open')) {
        setOpen(false);
        $('page-title').focus();
      }
    });
    window.addEventListener('hashchange', () => { if (drawer.hasAttribute('data-open')) setOpen(false); });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && drawer.hasAttribute('data-open')) {
        setOpen(false);
        toggle.focus();
      }
    });
  }

  return {bindMenu, display, liveRoute, resolve, sync};
})();
