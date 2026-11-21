'use strict';

const portalProjects = (() => {
  const ui = portalUI;
  const groups = [
    ['ready_for_review', '待审核'], ['waiting_on_you', '等你处理'], ['working', '进行中'],
    ['landing', '合入中'], ['idle', '空闲'], ['resolved', '已完成'],
  ];
  const tabs = [['threads', '线程'], ['library', '文件库'], ['usage', '用量'], ['settings', '设置']];
  const statusLabels = {active: '活跃', paused: '已暂停', archived: '已归档'};
  const documentsPath = '/knowledge/documents';
  const libraryPageSize = 50;
  const names = new Map();
  const projectDialog = document.getElementById('project-dialog');
  let loadVersion = 0, loading = false;
  let pressedBackdrop = false;

  const path = id => '/projects/' + enc(id);
  const href = (id, tab = 'threads') => '#' + path(id) + (tab === 'threads' ? '' : '/' + tab);
  const threadHref = id => '#/tasks/' + enc(id);
  const number = new Intl.NumberFormat('zh-CN');

  function closeDialog() {
    if (projectDialog.open) projectDialog.close();
    clearDialog();
  }

  function clearDialog() {
    $('project-dialog-title').textContent = '';
    $('project-dialog-content').replaceChildren();
  }

  function dialog(title, ...content) {
    $('project-dialog-title').textContent = title;
    $('project-dialog-content').replaceChildren(...content);
    pressedBackdrop = false;
    if (!projectDialog.open) projectDialog.showModal();
  }

  function onBackdrop(event) {
    if (event.target !== projectDialog) return false;
    const {left, top, right, bottom} = projectDialog.getBoundingClientRect();
    return event.clientX < left || event.clientX >= right || event.clientY < top || event.clientY >= bottom;
  }

  projectDialog.addEventListener('pointerdown', event => { pressedBackdrop = onBackdrop(event); });
  projectDialog.addEventListener('click', event => {
    if (pressedBackdrop && onBackdrop(event)) closeDialog();
    pressedBackdrop = false;
  });
  projectDialog.addEventListener('close', () => {
    if (!projectDialog.open) clearDialog();
  });

  function leave() {
    loadVersion++;
    loading = false;
    closeDialog();
  }

  function reset() {
    leave();
    names.clear();
    for (const id of ['project-cards', 'project-content', 'project-actions', 'projects-status']) {
      $(id).replaceChildren();
    }
    $('task-project-breadcrumb').hidden = true;
  }

  function showHome(projects) {
    const cards = [...projects].sort((a, b) => Number(a.status === 'archived') - Number(b.status === 'archived'));
    $('project-cards').replaceChildren(...(cards.length ? cards.map(project => {
      names.set(project.id, project.name);
      const card = el('article', undefined, 'panel project-card');
      const header = el('div', undefined, 'panel-head');
      const title = el('h2');
      title.append(evidenceLink(project.name, href(project.id)));
      header.append(title, el('span', statusLabels[project.status], 'badge'));
      const counts = el('dl', undefined, 'project-counts');
      counts.append(...groups.map(([key, label]) => {
        const item = el('div');
        item.append(el('dt', label), el('dd', number.format(project.group_counts[key] || 0)));
        return item;
      }));
      card.append(header);
      if (project.goal) card.append(el('p', project.goal, 'portal-wrap'));
      card.append(counts, el('small', '最近活动 · ' + ui.date(project.last_activity_at)));
      return card;
    }) : [el('p', '暂无项目。', 'empty-state')]));
    $('project-create').onclick = () => {
      if (ui.can('member')) dialog('新建项目', projectForm());
    };
  }

  function editorRepositories(repos) {
    const block = el('fieldset', undefined, 'portal-disclosure stack');
    const rows = el('div', undefined, 'stack');
    const add = button('添加仓库', () => addRow({}));
    function addRow(repo) {
      const row = ui.fields(
        ui.field('仓库地址', 'repository', {required: true, maxLength: 2000, value: repo.repo_url || ''}),
        ui.field('基线分支、标签或提交', 'ref', {maxLength: 255, value: repo.base_ref || 'main'}),
      );
      row.append(button('移除仓库', () => {
        row.remove();
        add.disabled = false;
        add.focus();
      }));
      rows.append(row);
      add.disabled = rows.children.length >= 32;
      return row;
    }
    repos.forEach(addRow);
    block.append(el('legend', '仓库'), el('small', '首个仓库为新线程的默认仓库。'), rows, add);
    return {block, value: () => [...rows.children].map(row => ({
      repo_url: row.querySelector('[name=repository]').value.trim(),
      base_ref: row.querySelector('[name=ref]').value.trim() || 'main',
    }))};
  }

  function projectForm(project, afterSave) {
    const repos = editorRepositories(project?.repos || []);
    const instructions = ui.field('项目指令', 'instructions', {
      multiline: true, rows: 8, value: project?.instructions || '',
    });
    const counter = el('small');
    counter.id = project ? 'project-settings-length' : 'project-create-length';
    const input = instructions.querySelector('textarea');
    input.setAttribute('aria-describedby', counter.id);
    const updateCounter = () => {
      const length = Array.from(input.value).length;
      counter.textContent = number.format(length) + ' / 16,000';
      input.setCustomValidity(length > 16000 ? '项目指令最多 16,000 字符。' : '');
    };
    input.oninput = updateCounter;
    instructions.append(counter);
    updateCounter();
    return ui.form([
      ui.fields(
        ui.field('项目名称', 'name', {required: true, maxLength: 200, value: project?.name || ''}),
        ui.field('目标', 'goal', {maxLength: 500, value: project?.goal || ''}),
        ui.field('默认后端', 'default_backend', {
          choices: ui.backendChoices(portalConfig, project?.default_backend), value: project?.default_backend || '',
        }),
        ui.field('默认模型', 'default_model', {maxLength: 200, value: project?.default_model || '',
          placeholder: portalConfig.default_model}),
      ),
      repos.block, instructions,
    ], project ? '保存设置' : '创建项目', async form => {
      if (!ui.can('member')) return;
      const identity = portalIdentity;
      const clean = name => ui.value(form, name);
      const payload = {
        name: clean('name'), goal: clean('goal') || null, instructions: input.value, repos: repos.value(),
        default_backend: clean('default_backend') || null, default_model: clean('default_model') || null,
      };
      const saved = await data(project ? path(project.id) : '/projects', payload, project ? 'PATCH' : 'POST');
      if (identity !== portalIdentity) return;
      names.set(saved.id, saved.name);
      if (!project) {
        closeDialog();
        location.hash = href(saved.id);
      } else if (afterSave) {
        await afterSave();
        return '项目设置已保存。';
      } else if (location.hash === href(saved.id, 'settings')) {
        showHeader(saved, 'settings');
        return '项目设置已保存。';
      }
    });
  }

  function showHeader(project, tab) {
    names.set(project.id, project.name);
    $('project-name').textContent = project.name;
    $('project-goal').textContent = project.goal || '';
    $('project-goal').hidden = !project.goal;
    $('project-status').textContent = statusLabels[project.status];
    $('project-tabs').replaceChildren(...tabs.map(([id, label]) => {
      const link = evidenceLink(label, href(project.id, id));
      link.className = 'button';
      if (tab === id) link.setAttribute('aria-current', 'page');
      return link;
    }));
    $('project-actions').replaceChildren();
    if (!ui.can('member')) return;
    const identity = portalIdentity, route = location.hash;
    const actions = [button('设置', () => {
      if (!ui.can('member')) return;
      dialog('项目设置', projectForm(project, async () => {
        if (identity === portalIdentity && route === location.hash) await load([project.id, tab], true);
      }));
    })];
    const stateActions = {
      active: [['pause', '暂停'], ['archive', '归档']],
      paused: [['resume', '恢复'], ['archive', '归档']],
      archived: [['unarchive', '取消归档']],
    };
    if ($('project-actions').dataset.state !== project.status) $('project-menu').open = false;
    $('project-actions').dataset.state = project.status;
    for (const [action, label] of stateActions[project.status]) {
      actions.push(ui.action(label, async () => {
        if (!ui.can('member')) return;
        await data(path(project.id) + '/' + action, {});
        if (identity === portalIdentity && route === location.hash) {
          $('project-menu').open = false;
          await load([project.id, tab]);
        }
      }, $('projects-status')));
    }
    $('project-actions').append(...actions);
  }

  function threadLabel(task) {
    const prompt = task.spec.instruction || task.spec.question || '';
    const separator = '\n\n## Thread request\n\n';
    const request = prompt.startsWith('## Project instructions\n\n') && prompt.includes(separator)
      ? prompt.slice(prompt.indexOf(separator) + separator.length) : prompt;
    return request.trim().split(/\r?\n/u)[0].slice(0, 110) || '线程 ' + task.id.slice(0, 8);
  }

  function showThreads(root, project, threads) {
    if (project.status === 'active' && ui.can('member')) {
      const composer = ui.action('新线程', async () => {
        if (!ui.can('member')) return;
        const form = el('div');
        dialog('新线程', form);
        await portalTasks.loadSubmit(form, project);
      }, $('projects-status'));
      composer.classList.add('primary');
      root.append(ui.actions(composer));
    }
    const board = el('div', undefined, 'project-grid');
    board.append(...groups.map(([key, label]) => {
      const rows = threads.filter(thread => thread.group === key);
      const list = el('ul', undefined, 'project-thread-list');
      list.append(...rows.map(thread => {
        const row = el('li');
        row.append(evidenceLink(threadLabel(thread), threadHref(thread.id)),
          el('small', statusText(thread.state) + ' · ' + ui.date(thread.updated_at)));
        return row;
      }));
      return ui.panel(label + ' · ' + rows.length, rows.length ? list : el('p', '暂无线程。', 'empty-state'));
    }));
    root.append(board);
  }

  function showSettings(root, project) {
    if (ui.can('member')) root.append(ui.panel('项目设置', projectForm(project)));
    else {
      const repos = el('ul', undefined, 'portal-paths');
      repos.append(...project.repos.map(repo => el('li', repo.repo_url + ' · ' + repo.base_ref)));
      root.append(ui.panel('项目设置',
        table(['默认后端', '默认模型'], [[project.default_backend || '平台默认', project.default_model || '平台默认']]),
        el('h3', '仓库'), project.repos.length ? repos : el('p', '暂无仓库。'),
        el('h3', '项目指令'), el('pre', project.instructions || '暂无指令。', 'project-doc-text')));
    }
    if (!ui.can('admin')) return;
    root.append(ui.actions(button('删除项目', () => {
      const feedback = el('p', undefined, 'portal-message');
      dialog('删除项目', el('p', '确认删除「' + project.name + '」？'), ui.action('确认删除', async () => {
        if (!ui.can('admin')) return;
        const identity = portalIdentity;
        await api(path(project.id), undefined, 'DELETE');
        if (identity === portalIdentity) {
          names.delete(project.id);
          closeDialog();
          location.hash = '#/projects';
        }
      }, feedback), feedback);
    })));
  }

  function uploadDocument(project) {
    return ui.form([
      ui.fields(
        ui.field('文档标题', 'title', {maxLength: 255}),
        ui.field('格式', 'format', {choices: [['text', '纯文本'], ['markdown', 'Markdown'], ['pdf-text', 'PDF 提取文本']]}),
        ui.field('文本文件', 'file', {type: 'file', accept: '.txt,.md,.markdown,.csv,.json,text/plain,text/markdown'}),
        ui.field('允许的团队（可选）', 'teams'),
        ui.field('文档正文', 'text', {multiline: true, rows: 8, maxLength: 100000, wide: true}),
      ),
    ], '上传文档', async form => {
      if (!ui.can('admin')) return;
      const identity = portalIdentity, route = location.hash;
      const file = ui.control(form, 'file').files[0], typed = ui.control(form, 'text').value;
      if (file && typed.trim()) throw Error('请选择文件或填写正文。');
      if (file?.size > 400000) throw Error('文档正文最多 10 万字符。');
      const text = file ? await file.text() : typed;
      const title = ui.value(form, 'title') || file?.name;
      if (!title || !text.trim()) throw Error('请提供标题和文档内容。');
      if (identity !== portalIdentity) return;
      await data(documentsPath, {project_id: project.id, title, text, format: ui.value(form, 'format'),
        allowed_teams: ui.items(ui.value(form, 'teams'))});
      if (identity === portalIdentity && route === location.hash) {
        closeDialog();
        await load([project.id, 'library']);
      }
    });
  }

  function showLibrary(root, project, library) {
    const documents = ui.scroll('项目知识文档'), artifacts = ui.scroll('线程产物');
    const feedback = el('p', undefined, 'portal-message');
    feedback.setAttribute('role', 'status');
    const identity = portalIdentity, route = location.hash;
    const current = () => identity === portalIdentity && route === location.hash && root.isConnected;
    let offset = 0;
    const page = delta => async () => {
      const position = offset + delta;
      const payload = await data(path(project.id) + '/library?limit=' + libraryPageSize + '&offset=' + position);
      if (current()) {
        offset = position;
        showDocuments(payload.documents);
      }
    };
    const previous = ui.action('上一页', page(-libraryPageSize), feedback);
    const next = ui.action('下一页', page(libraryPageSize), feedback);
    function showDocuments(rows) {
      ui.list(documents, ['文档', '范围', '上传时间'], rows.map(doc => [
        ui.action(doc.title, async () => {
          const detail = await data(documentsPath + '/' + enc(doc.id) + '?project_id=' + enc(project.id));
          if (current()) dialog(detail.title, detail.format === 'markdown'
            ? portalMarkdown.render(detail.text) : el('pre', detail.text, 'project-doc-text'));
        }, feedback),
        doc.project_id ? '项目' : '租户共享', ui.date(doc.created_at),
      ]), '暂无可见文档。');
      previous.hidden = offset === 0;
      next.hidden = rows.length < libraryPageSize;
    }
    showDocuments(library.documents);
    ui.list(artifacts, ['文件', '线程', '大小', '上传时间'], library.artifacts.map(item => [
      ui.action(item.name, async () => {
        const name = item.name.split('/').map(enc).join('/');
        const response = await api('/tasks/' + enc(item.task_id) + '/artifacts/' + name);
        const blob = await response.blob();
        if (!current()) return;
        const url = URL.createObjectURL(blob), link = el('a');
        link.href = url;
        link.download = item.name.split('/').at(-1);
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }, feedback),
      evidenceLink(item.task_id.slice(0, 8), threadHref(item.task_id)),
      number.format(item.size_bytes) + ' B', ui.date(item.created_at),
    ]), '暂无线程产物。');
    const actions = [previous, next];
    if (ui.can('admin')) actions.unshift(button('上传文档', () => {
      if (ui.can('admin')) dialog('上传项目文档', uploadDocument(project));
    }));
    root.append(ui.panel('知识文档', documents, ui.actions(...actions)),
      ui.panel('线程产物', artifacts), feedback);
  }

  function showUsage(root, usage) {
    const values = row => [
      number.format(row.turns), number.format(row.tokens_in), number.format(row.tokens_out),
      number.format(row.sandbox_seconds), ui.cost(row.cost_usd),
    ];
    const labels = ['轮次', '输入 Token', '输出 Token', '沙箱秒数', '费用'];
    const models = ui.scroll('按模型用量'), threads = ui.scroll('按线程用量');
    ui.list(models, ['模型', ...labels],
      Object.entries(usage.by_model).map(([model, row]) => [model, ...values(row)]), '暂无模型用量。');
    ui.list(threads, ['线程', '模型', ...labels], usage.by_thread.map(row => [
      evidenceLink(row.task_id.slice(0, 8), threadHref(row.task_id)),
      Object.keys(row.by_model).join('、') || row.model, ...values(row),
    ]), '暂无线程用量。');
    root.append(ui.panel('总用量', table(['线程', ...labels], [[number.format(usage.totals.tasks), ...values(usage.totals)]])),
      ui.panel('按模型', models), ui.panel('按线程', threads));
  }

  async function load(parts, quiet = false) {
    const version = ++loadVersion, route = location.hash, identity = portalIdentity;
    const current = () => version === loadVersion && route === location.hash && identity === portalIdentity;
    loading = true;
    if (!quiet) closeDialog();
    const id = parts[0] && decodeURIComponent(parts[0]);
    if (!quiet) {
      $('projects-home').hidden = true;
      $('project-detail').hidden = true;
      ui.setMessage($('projects-status'), '正在读取项目…');
    }
    try {
      if (!id) {
        const projects = await data('/projects');
        if (!current()) return;
        showHome(projects);
        $('projects-home').hidden = false;
      } else {
        const tab = tabs.some(([name]) => name === parts[1]) ? parts[1] : 'threads';
        const [project, payload] = await Promise.all([
          data(path(id)), tab === 'settings' ? null : data(path(id) + '/' + tab +
            (tab === 'library' ? '?limit=' + libraryPageSize : '')),
        ]);
        if (!current()) return;
        showHeader(project, tab);
        const root = el('div', undefined, 'page');
        if (tab === 'threads') showThreads(root, project, payload);
        if (tab === 'settings') showSettings(root, project);
        if (tab === 'library') showLibrary(root, project, payload);
        if (tab === 'usage') showUsage(root, payload);
        $('project-content').replaceChildren(root);
        $('project-detail').hidden = false;
      }
      if (!quiet) ui.setMessage($('projects-status'), '');
    } catch (err) {
      if (!current()) return;
      ui.setMessage($('projects-status'), err.message, true);
      if (err.status === 404) $('projects-status').append(' ', evidenceLink('返回项目列表', '#/projects'));
    } finally {
      if (version === loadVersion) loading = false;
    }
  }

  function poll(parts) {
    if (loading || (parts[1] && parts[1] !== 'threads') || projectDialog.open || $('project-menu').open) return;
    return load(parts, true);
  }

  function breadcrumb(task) {
    const key = task.project_id, link = $('task-project-link');
    $('task-project-breadcrumb').hidden = !key;
    if (!key) return;
    link.textContent = names.get(key) || '项目';
    link.href = names.get(key) === null ? '#/projects' : href(key);
    if (names.has(key)) return;
    const identity = portalIdentity;
    data(path(key)).then(project => {
      if (identity !== portalIdentity) return;
      names.set(key, project.name);
      if (currentTask?.id === task.id) link.textContent = project.name;
    }).catch(err => {
      if (identity === portalIdentity && err.status === 404) {
        names.set(key, null);
        if (currentTask?.id === task.id) link.href = '#/projects';
      }
    });
  }

  return {breadcrumb, leave, load, poll, reset};
})();
