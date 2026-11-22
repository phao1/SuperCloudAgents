'use strict';

const portalTasks = (() => {
  const ui = portalUI;
  const intentTypes = [
    ['openapi', 'OpenAPI 规范'], ['rules', '项目规则'], ['visual', '截图基准'],
    ['design_tokens', 'Design Tokens'], ['figma', 'Figma 设计稿'],
    ['github_issue', 'GitHub 工单'], ['jira', 'Jira 工单'], ['linear', 'Linear 工单'],
  ];
  let uploadedFiles = new WeakMap(), terminal = null, terminalTask = null, conversation = null;
  let outcomeSignature, attemptsSignature, impactSignature, reportCache, analysisSignature, refreshTools;

  function disclosure(title, ...children) {
    const details = el('details', undefined, 'portal-disclosure');
    details.append(el('summary', title), ...children);
    return details;
  }

  function intentFields(type) {
    const {field, fields, check} = ui;
    const secret = () => field('凭证引用', 'token_secret', {required: true, placeholder: '例如 design 或 tracker'});
    const readOnly = () => check('只读导入工单', 'read_only');
    if (type === 'figma') return fields(
      field('Figma 文件 Key 或链接', 'file_key', {required: true}), secret(),
      field('节点 ID（可选，逗号分隔）', 'node_ids', {placeholder: '12:34, 56:78'}),
      field('视觉检查和变量模式（JSON，可选）', 'params', {multiline: true, wide: true,
        placeholder: '{"visual":{"12:34":{"start_command":"npm run dev","base_url":"…","page_url":"…"}}}'})
    );
    if (type === 'jira') return fields(
      field('Jira 站点', 'site', {type: 'url', required: true}),
      field('工单编号', 'issue_key', {required: true, placeholder: 'WEB-123'}), secret(),
      field('验收自定义字段（可选）', 'acceptance_field', {placeholder: 'customfield_10037'}),
      field('成功时的流转 ID（可选）', 'transition_id'), readOnly()
    );
    if (type === 'linear') return fields(
      field('Linear 工单标识', 'issue_id', {required: true, placeholder: 'WEB-124'}), secret(),
      field('成功时的状态 ID（可选）', 'state_id'), readOnly()
    );
    if (type === 'github_issue') return fields(
      field('GitHub 仓库', 'repo', {required: true, placeholder: 'owner/repo'}),
      field('工单编号', 'number', {type: 'number', min: 1, required: true}), readOnly()
    );
    return fields(
      field('上传文件', 'file', {type: 'file'}),
      field('仓库内相对路径或已上传文件 ID', 'reference'),
      field('路径来源', 'reference_type', {choices: [['path', '仓库路径'], ['file_id', '已上传文件 ID']]}),
      ...(type === 'design_tokens' ? [check('生成 CSS 变量', 'css_variables')] : []),
      field('来源参数（JSON，可选）', 'params', {multiline: true, wide: true,
        note: type === 'visual' ? '截图检查使用 page_url、start_command 和 base_url。'
          : '上传文件与路径引用选填一项。'})
    );
  }

  function addIntent(list, add, kind) {
    const row = disclosure('意图来源', ui.fields(
      ui.field('来源类型', 'type', {choices: intentTypes, value: kind}),
      ui.field('名称', 'name', {required: true, maxLength: 31, pattern: '[a-z][a-z0-9\\-]{0,30}',
        placeholder: '例如 checkout-design'})
    ));
    row.classList.add('portal-intent');
    row.open = true;
    const extras = el('div', undefined, 'stack');
    const remove = ui.action('移除此来源', () => {
      row.remove();
      add.disabled = false;
      add.focus();
    });
    const types = row.querySelector('[name="type"]');
    const update = () => {
      row.querySelector('summary').textContent = intentTypes.find(([key]) => key === types.value)[1];
      extras.replaceChildren(intentFields(types.value));
    };
    types.onchange = update;
    row.append(extras, remove);
    update();
    list.append(row);
    add.disabled = list.children.length >= 16;
    row.querySelector('[name="name"]').focus();
  }

  function readIntent(row) {
    const input = name => row.querySelector('[name="' + name + '"]');
    const value = name => input(name)?.value.trim() || undefined;
    const type = value('type'), name = value('name');
    if (type === 'figma') {
      const key = value('file_key');
      const link = key?.match(/^https:\/\/(?:www\.)?figma\.com\/(?:file|design|proto)\/([A-Za-z0-9_-]+)(?:[/?#]|$)/u);
      const nodes = ui.items(value('node_ids') || '');
      return {source: {type, name, params: {
        ...ui.object(value('params'), 'Figma 参数'), file_key: link?.[1] || key,
        token_secret: value('token_secret'), ...(nodes.length && {node_ids: nodes}),
      }}};
    }
    if (['jira', 'linear', 'github_issue'].includes(type)) {
      const params = {writeback: !input('read_only').checked};
      for (const key of ['site', 'issue_key', 'issue_id', 'token_secret', 'acceptance_field',
        'transition_id', 'state_id', 'repo']) {
        if (value(key)) params[key] = value(key);
      }
      if (type === 'github_issue') params.number = Number(value('number'));
      return {source: {type, name, params}};
    }
    const file = input('file').files[0], reference = value('reference');
    if (Boolean(file) === Boolean(reference)) throw Error('请为「' + name + '」选择一个文件或填写一个路径引用。');
    if (file?.size > 2 * 1024 * 1024) throw Error('意图文件「' + file.name + '」超过 2 MiB。');
    const params = ui.object(value('params'), '意图参数');
    if (input('css_variables')?.checked) params.css_variables = true;
    return {source: {type, name, params, ...(reference && {[value('reference_type')]: reference})}, file};
  }

  function submitFields(form, intentList, defaultBackend) {
    const v = name => ui.value(form, name);
    const c = name => ui.checked(form, name);
    const optional = name => v(name) || undefined;
    const best = Number(v('best_n')), planMode = v('plan_mode');
    const spec = {
      repo_url: optional('repo_url'), base_ref: optional('base_ref'), instruction: v('instruction'),
      verify_commands: ui.lines(v('verify_commands')), setup_command: optional('setup_command'),
      model: optional('model'), agent_backend: optional('agent_backend'),
      image: optional('image'), push: c('push'), fork_from_snapshot: c('fork_from_snapshot'),
      plan: planMode === 'auto' ? 'auto' : 'none',
      ...(v('max_cost_usd') && {max_cost_usd: Number(v('max_cost_usd'))}),
      ...(v('max_tokens') && {max_tokens: Number(v('max_tokens'))}),
    };
    const backend = spec.agent_backend || defaultBackend;
    if (backend?.startsWith('opencode')) spec.agent_version = optional('agent_version');
    if (backend === 'opencode') {
      spec.tools = new FormData(form).getAll('tool');
    }
    if ((backend === 'opencode' || backend === 'dsh') && v('champion')) spec.use_champion = v('champion') === 'yes';
    if (spec.push && c('open_pr')) spec.pull_request = {
      title: optional('pr_title'), body: optional('pr_body'), base: optional('pr_base'),
      draft: c('pr_draft'), open_on: v('open_on'), merge_on_approve: c('merge_on_approve'),
    };
    if (best > 1) spec.best_of = {
      n: best, select: v('best_select'),
      models: v('best_models') ? ui.items(v('best_models')) : undefined,
      agent_versions: v('best_versions') ? ui.items(v('best_versions')) : undefined,
      per_attempt_max_cost_usd: v('best_costs') ? ui.items(v('best_costs')).map(Number) : undefined,
    };
    if (planMode !== 'none') {
      spec.shared_paths = ui.lines(v('shared_paths'));
      if (planMode === 'auto') spec.plan_review = c('plan_review');
      else {
        try {
          spec.subtasks = JSON.parse(v('subtasks'));
          if (!Array.isArray(spec.subtasks)) throw Error();
        } catch {
          throw Error('手动子任务须填写 JSON 数组。');
        }
      }
    }
    const intents = [...intentList.children].map(readIntent);
    if (new Set(intents.map(({source}) => source.name)).size !== intents.length) throw Error('意图名称必须唯一。');
    return {spec, intents};
  }

  async function loadSubmit(target = $('submit-form'), project = null) {
    if (!project && target.hasChildNodes()) return refreshTools();
    const identity = portalIdentity, hash = location.hash;
    const current = () => identity === portalIdentity && hash === location.hash && target.isConnected;
    await Promise.all([
      portalConfig || loadConfig(),
      loadVersions().catch(err => { if (current()) error(err); }),
    ]);
    if (!current()) return;
    const {check, control, field, fields, value} = ui;
    const primaryRepo = project?.repos[0];
    const defaultBackend = project?.default_backend || portalConfig.default_agent_backend;
    const defaultModel = project?.default_model || portalConfig.default_model;
    const available = el('div', undefined, 'stack');
    const versionsList = el('datalist');
    versionsList.id = project ? 'project-submit-agent-versions' : 'submit-agent-versions';
    versionsList.append(...versions.map(version => {
      const option = el('option');
      option.value = version.version;
      return option;
    }));
    const list = el('div', undefined, 'stack');
    const addKind = field('意图类型', 'add_intent_type', {choices: intentTypes});
    const add = ui.action('添加意图来源', () => addIntent(list, add, addKind.querySelector('select').value));
    const pr = fields(
      field('PR 标题', 'pr_title', {maxLength: 250}), field('PR 目标分支', 'pr_base'),
      field('创建时机', 'open_on', {choices: [['review', '等待审核时'], ['approve', '审核通过时']]}),
      check('草稿 PR', 'pr_draft'), check('批准后尝试合并', 'merge_on_approve'),
      field('PR 说明', 'pr_body', {multiline: true, wide: true, maxLength: 20000})
    );
    const prOption = check('创建拉取请求（PR）', 'open_pr');
    const bestOptions = fields(
      field('选择方式', 'best_select', {choices: [['verify_then_cost', '按验证结果与费用自动选择'], ['human', '人工选择']]}),
      field('尝试模型（可选，逗号分隔）', 'best_models'),
      field('尝试 OpenCode 版本（可选，逗号分隔）', 'best_versions'),
      field('每次尝试的预算（美元，可选）', 'best_costs', {note: '按尝试顺序填写金额，以逗号分隔。'})
    );
    const bestModels = el('datalist');
    bestModels.id = project ? 'project-best-models' : 'best-models';
    const planOptions = fields(
      field('共享路径（每行一个仓库相对路径）', 'shared_paths', {multiline: true}),
      field('子任务配置（JSON）', 'subtasks', {multiline: true, note: '每项填写 name、instruction，可填 depends_on 与 paths。'}),
      check('规划后等待人工确认', 'plan_review')
    );
    const form = ui.form([
      fields(
        field('仓库地址', 'repo_url', {required: !primaryRepo,
          placeholder: primaryRepo?.repo_url || 'Git URL 或服务端仓库路径'}),
        field('基线分支、标签或提交', 'base_ref', {value: project ? '' : 'main', required: !project,
          placeholder: primaryRepo?.base_ref || 'main'}),
        field(project ? '线程指令' : '任务指令', 'instruction', {required: true, multiline: true, wide: true}),
        field('准备命令（可选）', 'setup_command'),
        portalModels.field('模型（可选）', 'model', {placeholder: defaultModel}),
        field('验证命令（每行一条，最多 16 条）', 'verify_commands', {multiline: true, wide: true}),
        field('Agent 后端', 'agent_backend', {choices: ui.backendChoices({
          ...portalConfig, default_agent_backend: defaultBackend,
        })}),
        field('OpenCode 版本（可选）', 'agent_version', {placeholder: '平台默认版本'})
      ),
      disclosure('推送与拉取请求', check('推送任务分支', 'push'), prOption, pr),
      disclosure('Best-of-N 与执行设置', fields(
        field('并行尝试数', 'best_n', {choices: [[1, '1 · 单次执行'], ...[2, 3, 4, 5].map(n => [n, String(n)])]}),
        field('任务预算（美元）', 'max_cost_usd', {type: 'number', min: 0, step: 'any'}),
        field('任务预算（Token）', 'max_tokens', {type: 'number', min: 1}),
        field('任务拆分', 'plan_mode', {choices: [['none', '无'], ['auto', '自动规划'], ['manual', '手动子任务']]}),
        field('冠军覆盖', 'champion', {choices: [['', '按租户灰度'], ['yes', '使用当前冠军'], ['no', '使用基线']]}),
        field('沙箱镜像（可选）', 'image'),
        check('复用已准备的工作区快照', 'fork_from_snapshot')
      ), bestOptions, bestModels, planOptions),
      disclosure('工具', el('small', '已授权的 MCP 工具可附加到 OpenCode 任务。'), available),
      disclosure('设计意图与工单', ui.actions(addKind, add), list),
      versionsList,
    ], project ? '提交线程' : '提交任务', async form => {
      const {spec, intents} = submitFields(form, list, defaultBackend);
      const token = portalToken;
      spec.intents = [];
      for (const {source, file} of intents) {
        if (token !== portalToken) return;
        if (file) {
          const id = uploadedFiles.get(file) || (await ui.upload(file)).id;
          if (token !== portalToken) return;
          uploadedFiles.set(file, id);
          source.file_id = id;
        }
        spec.intents.push(source);
      }
      if (token !== portalToken) return;
      const task = await data('/tasks', {kind: 'coding', spec, ...(project && {project_id: project.id})});
      if (token !== portalToken) return;
      form.reset();
      for (const input of available.querySelectorAll('input')) input.checked = false;
      list.replaceChildren();
      add.disabled = false;
      sync();
      await openTask(task.id);
      return '任务已创建：' + task.id;
    });
    form.classList.add('portal-form');
    if (primaryRepo) {
      const repositories = el('datalist');
      repositories.id = 'project-submit-repositories';
      repositories.append(...project.repos.map(repo => {
        const option = el('option', repo.base_ref);
        option.value = repo.repo_url;
        return option;
      }));
      form.append(repositories);
      control(form, 'repo_url').setAttribute('list', repositories.id);
    }
    control(form, 'agent_version').setAttribute('list', versionsList.id);
    portalModels.multipleAliases(control(form, 'best_models'), bestModels);
    portalModels.bindBudget(form, () => {
      const explicit = Number(value(form, 'best_n')) > 1 && ui.items(value(form, 'best_models'));
      return explicit?.length ? explicit : [value(form, 'model') || project?.default_model || portalConfig?.default_model];
    });
    const sync = () => {
      const backend = value(form, 'agent_backend') || defaultBackend;
      control(form, 'agent_version').disabled = !backend?.startsWith('opencode');
      control(form, 'champion').disabled = backend !== 'opencode' && backend !== 'dsh';
      for (const input of available.querySelectorAll('input')) input.disabled = backend !== 'opencode';
      prOption.hidden = !ui.checked(form, 'push');
      pr.hidden = prOption.hidden || !ui.checked(form, 'open_pr');
      bestOptions.hidden = Number(value(form, 'best_n')) < 2;
      const mode = value(form, 'plan_mode');
      planOptions.hidden = mode === 'none';
      control(form, 'subtasks').parentElement.hidden = mode !== 'manual';
      control(form, 'plan_review').parentElement.hidden = mode !== 'auto';
    };
    for (const name of ['agent_backend', 'push', 'open_pr', 'best_n', 'plan_mode']) control(form, name).onchange = sync;
    sync();
    target.replaceChildren(form);
    available.append(el('small', '正在读取可用工具…'));
    const refresh = async () => {
      try {
        const tools = await data('/tools/available');
        if (identity !== portalIdentity) return;
        const selected = new Set([...available.querySelectorAll('input:checked')].map(input => input.value));
        available.classList.remove('portal-problem');
        available.replaceChildren(...(tools.length ? tools.map(tool => {
          const label = check(tool.name + (tool.description ? ' · ' + tool.description : ''), 'tool', selected.has(tool.name));
          label.querySelector('input').value = tool.name;
          return label;
        }) : [el('small', '当前没有已授权的工具。')]));
        sync();
      } catch (e) {
        ui.setMessage(available, e.message, true);
      }
    };
    if (!project) refreshTools = refresh;
    return refresh();
  }

  function taskPath(task) {
    return '/tasks/' + enc(task.id);
  }

  function openTaskIs(id) {
    return ui.can('history') && selectedTask === id && location.hash.startsWith('#/tasks');
  }

  function paths(entries) {
    if (!entries?.length) return el('small', '无');
    const list = el('ul', undefined, 'portal-paths');
    list.append(...entries.map(path => el('li', path)));
    return list;
  }

  function delivery(task, root) {
    const {pr, issue, ticket_writeback: writeback} = task.result;
    const state = el('small', pr ? 'PR 状态 · ' + prState(pr.state) : '尚未创建 PR');
    const rows = [];
    if (pr) rows.push(ui.externalLink('拉取请求 #' + pr.number + ' ↗', pr.url));
    if (issue) rows.push(ui.externalLink('来源工单 #' + issue.number + ' ↗', issue.url));
    for (const source of task.spec.intents || []) {
      const params = source.params || {};
      if (source.type === 'jira' && typeof params.site === 'string' && params.issue_key) {
        rows.push(ui.externalLink('Jira ' + params.issue_key + ' ↗', params.site.replace(/\/$/u, '') + '/browse/' + enc(params.issue_key)));
      } else if (source.type === 'github_issue' && typeof params.repo === 'string' && params.number) {
        const repo = params.repo.split('/').map(enc).join('/');
        rows.push(ui.externalLink('GitHub 工单 #' + params.number + ' ↗', 'https://github.com/' + repo + '/issues/' + params.number));
      } else if (source.type === 'linear' && params.issue_id) {
        rows.push(el('span', 'Linear ' + params.issue_id));
      }
    }
    if (pr && ui.can('tenant')) rows.push(ui.action('刷新 PR 状态', async () => {
      const current = await data(taskPath(task) + '/pr');
      state.textContent = 'PR 状态 · ' + prState(current.state);
    }, $('task-extras-status')));
    root.replaceChildren(ui.actions(...rows));
    if (pr || task.spec.pull_request) root.append(state);
    if (writeback) {
      for (const [name, result] of Object.entries(writeback)) {
        root.append(el('small', name + ' · ' + (result.completed ? '已回写' : result.error || '待回写')));
      }
    }
    root.parentElement.hidden = !rows.length && !task.spec.pull_request && !writeback;
  }

  function prState(state) {
    return ({open: '已开启', closed: '已关闭', merged: '已合并'})[state] || '未知';
  }

  function render(task, events) {
    const result = task.result || {};
    const attached = events.findLast(event => event.type === 'tools.attached')?.payload.tools || [];
    const violations = events.filter(event => event.type === 'scope.violation');
    const applied = events.findLast(event => event.type === 'champion.applied')?.payload;
    const champion = result.champion_id || applied?.champion_id;
    const overlay = result.overlay_sha256 || applied?.overlay_sha256;
    const signature = JSON.stringify([task.id, result.pr, result.issue, result.ticket_writeback,
      attached, violations.map(event => event.seq), champion, overlay]);
    if (signature !== outcomeSignature) {
      outcomeSignature = signature;
      delivery(task, $('task-delivery'));
      const root = $('task-tools-champion');
      const names = attached.length ? attached.map(tool => tool.name + ' · v' + tool.version) : task.spec.tools || [];
      root.parentElement.hidden = !names.length && !champion;
      root.replaceChildren();
      if (names.length) root.append(el('p', (attached.length ? '已附加工具 · ' : '已请求工具 · ') + names.join('、')));
      if (champion) root.append(ui.can('eval-admin')
        ? evidenceLink('应用冠军 ' + champion, '#/evolutions/champions/' + enc(champion))
        : el('p', '应用冠军 · ' + champion));
      if (champion && overlay) root.append(el('small', '覆盖 SHA-256 · ' + overlay, 'portal-wrap'));
      const scope = $('task-scope');
      scope.parentElement.hidden = !violations.length;
      ui.list(scope, ['步骤', '子任务 / 轮次', '越界路径'], violations.map(event => [
        evidenceLink('事件 #' + event.seq, '#/tasks/' + enc(task.id) + '/events/' + event.seq),
        [event.payload.subtask, event.payload.turn !== undefined ? '第 ' + event.payload.turn + ' 轮' : null]
          .filter(Boolean).join(' · ') || '—',
        paths(event.payload.paths),
      ]));
    }
    const backend = task.spec.agent_backend || portalConfig?.default_agent_backend;
    $('task-terminal-panel').hidden = !ui.can('tenant') || backend !== 'opencode-brain'
      || (finishedTask(task.state) && terminalTask !== task.id);
    $('task-terminal-start').disabled = Boolean(terminal) || finishedTask(task.state);
    $('task-terminal-start').onclick = () => void startTerminal(task);
    $('task-terminal-stop').onclick = () => stopTerminal();
    if (terminal && finishedTask(task.state)) stopTerminal('任务已结束。');
    if (!archivePortal) renderConversation(task);
  }

  function renderConversation(task) {
    const identity = portalIdentity;
    if (conversation?.id !== task.id || conversation?.identity !== identity) {
      conversation?.panel.destroy();
      const panel = portalConversation.create($('task-conversation'), {
        path: taskPath(task), type: 'task', canWrite: () => ui.can('member'),
        active: () => portalIdentity === identity && openTaskIs(task.id),
        focus: id => taskDag?.focus(id),
      });
      conversation = {id: task.id, identity, panel};
    }
    conversation.panel.setState(finishedTask(task.state) ? '任务已结束，无法发送插话。' : '');
  }

  async function loadAttempts(task, artifactsRequest) {
    const root = $('task-best-of');
    root.parentElement.hidden = !task.spec.best_of;
    if (!task.spec.best_of) return;
    const key = JSON.stringify([task.id, task.state, Object.entries(task.result)
      .filter(([name]) => name.startsWith('best_of_'))]);
    if (attemptsSignature === key) return;
    const path = taskPath(task) + '/best-of';
    const [record, artifacts] = await Promise.all([data(path), artifactsRequest]);
    if (!openTaskIs(task.id)) return;
    const message = $('task-best-of-status');
    ui.list(root, ['尝试', '模型 / 版本', '验证', '费用', '轮次', '变更统计', '操作'],
      record.attempts.map(attempt => {
        const actions = [];
        if (attempt.patch_artifact) {
          const artifact = artifacts.find(item => item.name === attempt.patch_artifact);
          if (artifact?.missing_reason) {
            actions.push(el('span', artifact.missing_reason));
          } else {
            actions.push(ui.action('查看补丁', async () => {
              const name = attempt.patch_artifact.split('/').map(enc).join('/');
              const patch = await (await api(taskPath(task) + '/artifacts/' + name)).text();
              if (!openTaskIs(task.id)) return;
              $('task-attempt-patch-title').textContent = '候选 ' + attempt.attempt + ' 的补丁';
              diff($('task-attempt-patch-text'), patch);
              $('task-attempt-patch').hidden = false;
            }, message));
          }
          if (ui.can('member') && record.select === 'human' && record.awaiting_selection) {
            actions.push(ui.action('选择', async () => {
              await data(path + '/select', {attempt: attempt.attempt});
              ui.setMessage(message, '已提交候选选择，等待发布补丁。');
              await updateTask();
            }, message));
          }
        }
        const stats = attempt.diff_stats;
        const verification = el('div', attempt.verification ? (attempt.verification.passed ? '通过' : '未通过')
          : attempt.completed ? '未验证' : '等待结果');
        if (attempt.reason) verification.append(el('small', attempt.reason));
        return [
          attempt.attempt + (attempt.chosen ? ' · 已选中' : ''),
          [attempt.model, attempt.agent_version].filter(Boolean).join(' · ') || '默认配置',
          verification,
          ui.cost(attempt.cost_usd), attempt.turns,
          stats ? stats.files_changed + ' 文件 · +' + stats.insertions + ' / −' + stats.deletions : '—',
          ui.actions(...actions),
        ];
      }), '等待生成候选补丁。');
    if (record.chosen_attempt) ui.setMessage(message, '已选择候选 ' + record.chosen_attempt + '。');
    attemptsSignature = key;
  }

  function impact(task) {
    const root = $('task-impact');
    const artifact = task.result.patch_artifact;
    root.parentElement.hidden = !ui.can('tenant') || task.kind !== 'coding';
    if (root.parentElement.hidden) return;
    const key = JSON.stringify([task.id, artifact, task.result.commit]);
    if (impactSignature === key) return;
    impactSignature = key;
    if (!artifact) {
      root.replaceChildren(el('small', '发布补丁后可查看影响范围。'));
      return;
    }
    const message = el('p', undefined, 'status-text portal-message');
    message.setAttribute('role', 'status');
    root.replaceChildren(ui.actions(ui.action('分析已发布补丁', async () => {
      const result = await data(taskPath(task) + '/impact');
      if (impactSignature !== key || !openTaskIs(task.id)) return;
      root.replaceChildren(...[
        ['变更路径', result.changed_paths], ['受影响文件', result.impacted_files], ['建议测试', result.tests_to_run],
      ].map(([title, entries]) => {
        const section = el('div', undefined, 'stack');
        section.append(el('h4', title), paths(entries));
        return section;
      }));
    }, message)), message);
  }

  function refreshTask(task, artifactsRequest) {
    if (conversation?.id === task.id) void conversation.panel.refresh();
    impact(task);
    return loadAttempts(task, artifactsRequest).catch(e => {
      if (openTaskIs(task.id)) ui.setMessage($('task-best-of-status'), e.message, true);
    });
  }

  function stopTerminal(status) {
    const active = terminal;
    terminal = null;
    if (!active) return;
    active.controller.abort();
    $('task-terminal-status').textContent = status || '已停止观看。';
    $('task-terminal-stop').hidden = true;
    $('task-terminal-start').disabled = finishedTask(currentTask?.state);
  }

  async function startTerminal(task) {
    if (terminal || finishedTask(task.state)) return;
    terminalTask = task.id;
    const active = {controller: new AbortController()};
    terminal = active;
    const output = $('task-terminal-output'), info = $('task-terminal-status');
    $('task-terminal-start').disabled = true;
    $('task-terminal-stop').hidden = false;
    info.textContent = '等待沙箱终端输出…';
    output.textContent = '';
    const decoders = new Map();
    let buffer = '', log = '';
    const append = text => { log = (log + text).slice(-64000); };
    try {
      const response = await fetch('/v1' + taskPath(task) + '/terminal', {
        headers: {Authorization: 'Bearer ' + portalToken}, signal: active.controller.signal,
      });
      if (!response.ok) throw await apiFailure(response);
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      while (terminal === active) {
        const {done, value} = await reader.read();
        if (done || terminal !== active) break;
        buffer += value;
        const frames = buffer.split(/\r?\n\r?\n/u);
        buffer = frames.pop();
        for (const frame of frames) {
          const event = frame.match(/^event: (terminal\.(?:open|output|exit))$/mu)?.[1];
          if (!event) continue;
          const packet = JSON.parse(frame.match(/^data: (.*)$/mu)[1]);
          const session = packet.session_id;
          if (event === 'terminal.open') {
            info.textContent = '终端已连接 · 实时输出';
            decoders.set(session, new TextDecoder());
            append('\n── 终端会话 ──\n');
          } else if (event === 'terminal.output') {
            const decoder = decoders.get(session) || new TextDecoder();
            decoders.set(session, decoder);
            append(decoder.decode(Uint8Array.from(atob(packet.data), ch => ch.charCodeAt(0)), {stream: true}));
          } else {
            append((decoders.get(session)?.decode() || '') + '\n── 退出码 ' + (packet.exit_code ?? '—')
              + (packet.timed_out ? ' · 超时' : '')
              + (packet.truncated ? ' · 输出已截断' : '') + ' ──\n');
            decoders.delete(session);
          }
        }
        const atEnd = output.scrollHeight - output.scrollTop - output.clientHeight < 20;
        output.textContent = terminalLines(log.replace(/\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])|[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]/gu, ''));
        if (atEnd) output.scrollTop = output.scrollHeight;
      }
      if (terminal === active) stopTerminal('终端流已结束。');
    } catch (e) {
      if (terminal === active) stopTerminal(e.message);
    }
  }

  async function report(task, artifacts) {
    const artifact = artifacts.find(item => item.name === 'report.md');
    if (!artifact) return null;
    const key = JSON.stringify([task.id, artifact.created_at, artifact.size_bytes, artifact.missing_reason]);
    if (artifact.missing_reason) return {key, missingReason: artifact.missing_reason};
    if (key !== reportCache?.key) {
      const pending = api(taskPath(task) + '/artifacts/report.md').then(response => response.text());
      reportCache = {key, pending};
      pending.catch(() => { if (reportCache?.pending === pending) reportCache = null; });
    }
    return {key, text: await reportCache.pending};
  }

  function renderAnalysisChoices() {
    const rows = serverTasks.filter(task => task.kind === 'analysis');
    const signature = JSON.stringify(rows.map(task => [task.id, task.state]));
    if (analysisSignature === signature) return;
    analysisSignature = signature;
    ui.list($('analysis-recent'), ['问题', '状态', '提交时间', '任务'], rows.map(task => [
      evidenceLink(task.spec.question?.slice(0, 110) || task.id, '#/analysis/' + enc(task.id)),
      status(task.state), ui.date(task.created_at),
      evidenceLink('查看进度 ↗', '#/tasks/' + enc(task.id)),
    ]), '尚无分析任务。');
  }

  async function showAnalysis(id) {
    const root = $('analysis-page-report'), target = $('analysis-rendered-detail');
    root.hidden = !id;
    if (!id) return;
    const hash = location.hash, identity = portalIdentity;
    if (root.dataset.task !== id) {
      root.dataset.task = id;
      root.dataset.report = '';
      target.replaceChildren(el('p', '正在读取报告…'));
    }
    const task = await data('/tasks/' + enc(id));
    if (hash !== location.hash || identity !== portalIdentity) return;
    if (task.kind !== 'analysis') {
      target.replaceChildren(el('p', '请选择一个数据分析任务。'));
      return;
    }
    $('analysis-report-task').href = '#/tasks/' + enc(id);
    const artifacts = await data(taskPath(task) + '/artifacts');
    const result = await report(task, artifacts);
    if (hash !== location.hash || identity !== portalIdentity) return;
    if (!result) {
      root.dataset.report = '';
      target.replaceChildren(el('p', '分析报告尚未发布。当前状态 · ' + statusText(task.state)));
    } else if (root.dataset.report !== result.key) {
      target.replaceChildren(result.missingReason
        ? el('p', result.missingReason) : portalMarkdown.render(result.text));
      root.dataset.report = result.key;
    }
  }

  function resetTask() {
    stopTerminal();
    terminalTask = null;
    outcomeSignature = attemptsSignature = impactSignature = undefined;
    conversation?.panel.destroy();
    conversation = null;
    for (const id of ['task-delivery', 'task-tools-champion', 'task-scope', 'task-best-of', 'task-impact']) {
      $(id).replaceChildren();
      $(id).parentElement.hidden = true;
    }
    for (const id of ['task-extras-status', 'task-best-of-status', 'task-terminal-output', 'task-terminal-status',
      'task-attempt-patch-text', 'analysis-report', 'analysis-rendered', 'task-missing-artifacts']) $(id).replaceChildren();
    for (const id of ['task-attempt-patch', 'task-terminal-panel', 'analysis-report-panel',
      'task-missing-artifacts-panel']) $(id).hidden = true;
    $('analysis-report-panel').dataset.report = '';
  }

  function reset() {
    resetTask();
    uploadedFiles = new WeakMap();
    reportCache = null;
    analysisSignature = refreshTools = undefined;
    $('submit-form').replaceChildren();
    $('analysis-page-report').hidden = true;
    $('analysis-page-report').dataset.task = '';
    $('analysis-rendered-detail').replaceChildren();
    $('analysis-recent').replaceChildren();
  }

  return {loadSubmit, refreshTask, render, renderAnalysisChoices, report, reset, resetTask, showAnalysis, stopTerminal};
})();
