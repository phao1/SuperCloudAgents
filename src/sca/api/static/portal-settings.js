'use strict';

const portalSettings = (() => {
  const ui = portalUI;
  const toolsPath = '/tools', documentsPath = '/knowledge/documents', scmPath = '/integrations/scm';
  const membersPath = '/tenant/members', tokensPath = '/tenant/tokens', tenantsPath = '/admin/tenants';
  const billingPath = '/tenant/billing';
  const roleLabels = {owner: '所有者', admin: '管理员', member: '成员', viewer: '只读'};
  const roles = ['member', 'viewer', 'admin'].map(role => [role, roleLabels[role]]);
  const formats = [['text', '纯文本'], ['markdown', 'Markdown'], ['pdf-text', 'PDF 提取文本']];
  const providers = [['github', 'GitHub'], ['gitea', 'Gitea']];
  const mounted = new Map();
  let dismissToken = null;

  function message() {
    const node = el('p', undefined, 'status-text portal-message');
    node.setAttribute('role', 'status');
    return node;
  }

  function toolsView(root) {
    const admin = ui.can('admin'), tenant = portalIdentity.tenant_id, shared = tenant === 'platform';
    const list = ui.scroll(admin ? '工具注册表' : '可用工具'), feedback = message();
    const editor = el('div'), grants = el('div'), controls = el('div', undefined, 'stack');
    root.append(ui.panel(admin ? 'MCP 工具注册表' : '已授权的 MCP 工具', list, feedback));
    if (admin) {
      controls.append(ui.panel('工具配置', editor), grants);
      grants.hidden = true;
      root.append(controls);
      editTool();
    }

    function editTool(row = null) {
      const path = row ? toolsPath + '/' + enc(row.name) : toolsPath;
      const remoteFields = ui.fields(
        ui.field('MCP 服务 URL', 'url', {type: 'url', value: row?.url || ''}),
        ui.field('请求头到 OpenBao 引用（JSON）', 'headers', {multiline: true,
          value: row && Object.keys(row.headers).length ? json(row.headers) : '',
          note: '例如 {"Authorization":"mcp-auth"}。填写 OpenBao 中的引用名称。'})
      );
      const form = ui.form([
        el('h3', row ? '编辑 · ' + row.name : '登记工具'),
        ui.fields(ui.field('工具名称', 'name', {required: true, value: row?.name || '', readOnly: Boolean(row),
          pattern: '[a-z][a-z0-9_\\-]{0,62}'}),
        ui.field('类型', 'kind', {choices: shared ? [['remote', '远程 MCP'], ['builtin', '内置知识库']]
          : [['remote', '远程 MCP']], value: row?.kind || 'remote'}),
        ui.field('说明', 'description', {maxLength: 1000, value: row?.description || ''}),
        ui.check('启用工具', 'enabled', row?.enabled ?? true)),
        remoteFields,
      ], row ? '保存工具' : '登记工具', async form => {
        const kind = ui.value(form, 'kind');
        const body = {kind, description: ui.value(form, 'description'), enabled: ui.checked(form, 'enabled'),
          url: kind === 'remote' ? ui.value(form, 'url') : null,
          headers: kind === 'remote' ? ui.object(ui.value(form, 'headers'), '请求头引用') : {}};
        if (!row) body.name = ui.value(form, 'name');
        await data(path, body, row ? 'PATCH' : 'POST');
        ui.setMessage(feedback, row ? '工具已更新。' : '工具已登记。');
        editTool();
        await loadTools();
      });
      const kind = ui.control(form, 'kind');
      kind.onchange = () => { remoteFields.hidden = kind.value !== 'remote'; };
      kind.onchange();
      if (row) form.append(ui.actions(ui.action('取消编辑', () => editTool())));
      editor.replaceChildren(form);
      form.querySelector('h3').tabIndex = -1;
      if (row) form.querySelector('h3').focus();
    }

    function manageGrants(tool) {
      grants.hidden = false;
      const path = toolsPath + '/' + enc(tool.name) + '/grants';
      const rows = ui.scroll('工具授权'), status = message();
      const target = shared ? ui.field('目标租户（留空使用 platform）', 'tenant_id') : null;
      const targetInput = target?.querySelector('input');
      const canWrite = tool.tenant_id === tenant || tool.name === 'knowledge';
      let ticket = 0;
      const getTarget = () => targetInput?.value.trim() || '';
      const load = async () => {
        const index = ++ticket, owner = getTarget(), query = owner ? '?tenant_id=' + enc(owner) : '';
        const list = await data(path + query);
        if (index !== ticket) return;
        ui.list(rows, ['授权范围', '主体', '操作'], list.map(row => [
          ({tenant: '租户', team: '团队', user: '用户'})[row.subject_type], row.subject,
          canWrite ? ui.action('撤销', async () => {
            if (!confirm('确认撤销这条工具授权？')) return;
            const parameters = new URLSearchParams({subject_type: row.subject_type, subject: row.subject});
            if (owner) parameters.set('tenant_id', owner);
            await api(path + '?' + parameters, undefined, 'DELETE');
            await load();
          }, status) : '—',
        ]), '暂无授权。');
      };
      const form = ui.form([ui.fields(
        ui.field('授权范围', 'subject_type', {choices: [['tenant', '租户'], ['team', '团队'], ['user', '用户']]}),
        ui.field('主体', 'subject', {note: '团队填写配置的团队名称，用户填写 OIDC sub 或令牌主体。'})
      )], '添加授权', async form => {
        const owner = getTarget(), kind = ui.value(form, 'subject_type');
        const subject = kind === 'tenant' ? (owner || tenant) : ui.value(form, 'subject');
        if (!subject) throw Error('请填写授权主体。');
        await data(path, {subject_type: kind, subject, ...(owner && {tenant_id: owner})});
        form.reset();
        sync();
        await load();
        return '授权已添加。';
      });
      const sync = () => {
        ui.control(form, 'subject').parentElement.hidden = ui.value(form, 'subject_type') === 'tenant';
      };
      ui.control(form, 'subject_type').onchange = sync;
      sync();
      form.hidden = !canWrite;
      grants.replaceChildren(ui.panel(tool.name + ' · 授权',
        ...(target ? [ui.actions(target, ui.action('查看目标租户授权', load, status))] : []),
        rows, form, status));
      load().catch(e => ui.setMessage(status, e.message, true));
    }

    async function loadTools() {
      const records = await data(admin ? toolsPath : toolsPath + '/available');
      ui.list(list, admin ? ['名称', '说明', '所属租户 / 版本', '状态', '操作'] : ['名称', '说明', '所属租户 / 版本'],
        records.map(tool => {
          const row = [tool.name, tool.description,
            tool.tenant_id + ' · v' + tool.version];
          if (!admin) return row;
          const owned = tool.tenant_id === tenant;
          const primary = owned || !records.some(other => other.tenant_id === tenant && other.name === tool.name);
          const path = toolsPath + '/' + enc(tool.name);
          return [...row, tool.enabled ? '启用' : '停用', ui.actions(
            primary && ui.action('授权', () => manageGrants(tool), feedback),
            owned && ui.action('编辑', async () => editTool(await data(path)), feedback),
            owned && ui.action('删除', async () => {
              if (!confirm('确认删除工具 ' + tool.name + '？')) return;
              await api(path, undefined, 'DELETE');
              grants.hidden = true;
              editTool();
              await loadTools();
            }, feedback)
          )];
        }), admin ? '尚无登记的工具。' : '当前没有已授权的工具。');
    }

    const loadKnowledge = knowledgeView(root, admin);
    return () => Promise.all([loadTools(), loadKnowledge()]);
  }

  function knowledgeView(root, admin) {
    const docs = ui.scroll('知识文档'), hits = el('div', undefined, 'stack'), feedback = message();
    const size = 50;
    let offset = 0;
    const previous = ui.action('上一页', async () => { offset -= size; await load(); }, feedback);
    const next = ui.action('下一页', async () => { offset += size; await load(); }, feedback);
    const search = ui.form([ui.fields(
      ui.field('搜索词', 'query', {required: true, maxLength: 2000}),
      ui.field('结果数量', 'k', {type: 'number', required: true, value: 5, min: 1, max: 20})
    )], '搜索知识库', async form => {
      const results = await data('/knowledge/search', {
        query: ui.value(form, 'query'), k: Number(ui.value(form, 'k')),
      });
      hits.replaceChildren(...(results.length ? results.map(row => {
        const card = el('article', undefined, 'suite-card');
        card.append(el('strong', row.heading ? row.title + ' · ' + row.heading : row.title),
          el('p', row.text, 'portal-preline'), el('small', '引用 · ' + row.citation));
        return card;
      }) : [el('p', '没有匹配的文档。')]));
    });
    root.append(ui.panel('知识库检索', search, hits),
      ui.panel('知识文档', docs, ui.actions(previous, next), feedback));
    next.hidden = previous.hidden = true;
    if (admin) root.append(ui.panel('上传知识文档', ui.form([ui.fields(
      ui.field('标题', 'title', {maxLength: 255, note: '留空时使用文件名。'}),
      ui.field('格式', 'format', {choices: formats}),
      ui.field('文本文件（可选）', 'file', {type: 'file', accept: '.txt,.md,.markdown,.csv,.json,text/plain,text/markdown',
        note: 'PDF 请先提取为文本文件。'}),
      ui.field('允许的团队（可选，逗号分隔）', 'allowed_teams', {note: '留空对租户内可见。'}),
      ui.field('文档正文（或选择上方文件）', 'text', {multiline: true, wide: true, maxLength: 100000})
    )], '上传文档', async form => {
      const file = ui.control(form, 'file').files[0];
      const typed = ui.control(form, 'text').value;
      if (file && typed.trim()) throw Error('请选择文件或填写正文。');
      if (file?.size > 400000) throw Error('文档正文最多 10 万字符。');
      const text = file ? await file.text() : typed;
      const title = ui.value(form, 'title') || file?.name;
      if (!title || !text.trim()) throw Error('请提供标题和文档内容。');
      await data(documentsPath, {title, text, format: ui.value(form, 'format'),
        allowed_teams: ui.items(ui.value(form, 'allowed_teams'))});
      form.reset();
      offset = 0;
      await load();
      return '文档已上传并建立索引。';
    })));

    async function load() {
      const results = await data(documentsPath + '?limit=' + size + '&offset=' + offset);
      if (!results.length && offset) {
        offset = Math.max(0, offset - size);
        return load();
      }
      ui.list(docs, ['标题 / ID', '格式', '允许访问', '分块', '上传时间', ...(admin ? ['操作'] : [])],
        results.map(row => {
          const name = el('div', row.title);
          name.append(el('small', row.id));
          return [name, formats.find(([key]) => key === row.format)?.[1] || row.format,
            row.allowed_teams.join('、') || '租户内', row.chunk_count, ui.date(row.created_at),
            ...(admin ? [ui.action('删除', async () => {
              if (!confirm('确认删除知识文档「' + row.title + '」？')) return;
              await api(documentsPath + '/' + enc(row.id), undefined, 'DELETE');
              hits.replaceChildren();
              await load();
            }, feedback)] : [])];
        }), '暂无可见文档。');
      previous.hidden = offset === 0;
      next.hidden = results.length < size;
    }
    return load;
  }

  function integrationsView(root) {
    const list = ui.scroll('SCM 集成'), editor = el('div'), feedback = message();
    const webhookTips = el('p', '在代码平台添加 JSON webhook，并订阅工单与工单评论事件。密钥由部署方配置，表单填写引用。');
    const hints = el('ul', undefined, 'portal-paths');
    hints.append(
      el('li', 'GitHub App 或 Gitea 仓库令牌由运维配置到 OpenBao。Webhook 签名引用由 SCA_SCM_WEBHOOK_SECRETS 配置。'),
      el('li', 'Figma、Jira 和 Linear 凭证由 Worker 的 SCA_INTENT_CREDENTIALS 管理，键名如 figma/design、jira/tracker、linear/tracker。'),
      el('li', 'GitHub 工单可使用 github_issue/owner/repo 或仓库凭证。提交时在意图来源中填写凭证引用。')
    );
    root.append(ui.panel('GitHub 与 Gitea', webhookTips, list, feedback),
      ui.panel('SCM 配置', editor), ui.panel('设计与工单凭证', hints));
    function edit(row = null) {
      const defaults = row?.default_spec || {}, pr = defaults.pull_request || {};
      const options = el('details', undefined, 'portal-disclosure');
      options.append(el('summary', '工单任务的默认规格'), ui.fields(
        ui.field('默认基线（留空使用仓库默认分支）', 'base_ref', {value: defaults.base_ref || ''}),
        ui.field('Agent 后端', 'agent_backend', {value: defaults.agent_backend || '',
          choices: ui.backendChoices(portalConfig, defaults.agent_backend)}),
        portalModels.field('默认模型（可选）', 'model', {value: defaults.model || '', placeholder: portalConfig.default_model}),
        ui.field('沙箱镜像（可选）', 'image', {value: defaults.image || ''}),
        ui.field('准备命令', 'setup_command', {value: defaults.setup_command || ''}),
        ui.field('主要验证命令', 'verify_command', {value: defaults.verify_command || ''}),
        ui.field('其它验证命令（每行一条）', 'verify_commands', {multiline: true,
          value: (defaults.verify_commands || []).join('\n')}),
        ui.field('触发标签（每行一个）', 'trigger_labels', {multiline: true,
          value: (defaults.trigger_labels || []).join('\n'), note: '留空可由新工单触发。'}),
        ui.field('PR 标题', 'title', {value: pr.title || '', maxLength: 250}),
        ui.field('PR 目标分支', 'base', {value: pr.base || ''}),
        ui.field('PR 创建时机', 'open_on', {value: pr.open_on || 'review',
          choices: [['review', '等待审核时'], ['approve', '审核通过时']]}),
        ui.check('创建为草稿', 'draft', pr.draft === true),
        ui.check('批准后尝试合并', 'merge_on_approve', pr.merge_on_approve === true),
        ui.field('PR 说明', 'body', {multiline: true, wide: true, value: pr.body || '', maxLength: 20000})
      ));
      const hook = ui.field('Webhook 地址', 'webhook_url', {readOnly: true});
      const input = hook.querySelector('input');
      const copy = ui.action('复制地址', async () => {
        const ok = await ui.copy(input.value, input);
        ui.setMessage(feedback, ok ? '已复制 webhook 地址。' : '已选中地址，请使用复制快捷键。');
      }, feedback);
      const form = ui.form([
        el('h3', row ? '编辑 · ' + row.repo_full_name : '添加仓库集成'),
        ui.fields(
          ui.field('代码平台', 'provider', {choices: providers, value: row?.provider || 'github'}),
          ui.field('仓库主机（可含端口）', 'host', {required: true, value: row?.host || 'github.com'}),
          ui.field('仓库全名', 'repo_full_name', {required: true, value: row?.repo_full_name || '', placeholder: 'owner/repo'}),
          ui.field('Webhook 密钥引用', 'webhook_secret_ref', {required: true, value: row?.webhook_secret_ref || ''}),
          ui.check('启用工单触发', 'enabled', row?.enabled ?? true)
        ), ui.actions(hook, copy), options,
      ], row ? '保存集成' : '添加集成', async form => {
        const v = name => ui.value(form, name);
        const pick = names => Object.fromEntries(names.map(name => [name, v(name) || null]));
        const body = {...pick(['provider', 'host', 'repo_full_name', 'webhook_secret_ref']),
          enabled: ui.checked(form, 'enabled'), default_spec: {
            ...pick(['base_ref', 'agent_backend', 'model', 'image', 'setup_command', 'verify_command']),
            verify_commands: ui.lines(v('verify_commands')), trigger_labels: ui.lines(v('trigger_labels')),
            pull_request: {...pick(['title', 'body', 'base', 'open_on']), draft: ui.checked(form, 'draft'),
              merge_on_approve: ui.checked(form, 'merge_on_approve')},
          }};
        await data(row ? scmPath + '/' + enc(row.id) : scmPath, body, row ? 'PUT' : 'POST');
        ui.setMessage(feedback, row ? 'SCM 集成已更新。' : 'SCM 集成已添加。');
        edit();
        await load();
      });
      const provider = ui.control(form, 'provider'), host = ui.control(form, 'host');
      const updateHook = () => { input.value = location.origin + '/v1/webhooks/' + provider.value; };
      provider.onchange = () => {
        if (provider.value === 'github') host.value = 'github.com';
        else if (host.value === 'github.com') host.value = '';
        updateHook();
      };
      updateHook();
      if (row) form.append(ui.actions(ui.action('取消编辑', () => edit())));
      editor.replaceChildren(form);
      if (row) {
        form.querySelector('h3').tabIndex = -1;
        form.querySelector('h3').focus();
      }
    }
    edit();

    async function load() {
      const rows = await data(scmPath);
      ui.list(list, ['仓库', '平台', '启用状态', '默认规格', '操作'], rows.map(row => {
        const path = scmPath + '/' + enc(row.id), defaults = row.default_spec || {};
        return [row.host + '/' + row.repo_full_name, providers.find(([name]) => name === row.provider)?.[1],
          row.enabled ? '启用' : '停用',
          ['基线 ' + (defaults.base_ref || '仓库默认'), '触发标签 ' + (defaults.trigger_labels?.join('、') || '全部')]
            .join(' · '),
          ui.actions(ui.action('编辑 / 查看 webhook', async () => edit(await data(path)), feedback),
            ui.action('删除', async () => {
              if (!confirm('确认删除该 SCM 集成？')) return;
              await api(path, undefined, 'DELETE');
              edit();
              await load();
            }, feedback)),
        ];
      }), '尚无 SCM 集成。');
    }

    return load;
  }

  function usageView(root, platform = false) {
    const path = platform ? '/admin/usage' : '/usage';
    const rows = ui.scroll('用量明细'), chart = el('div', undefined, 'stack'), feedback = message();
    const today = new Date().toISOString().slice(0, 10);
    let currentRange = '';
    const csv = platform ? null : evidenceLink('导出 CSV', '/v1/usage/export.csv');
    if (csv) {
      csv.className = 'button';
      csv.download = 'usage.csv';
      csv.onclick = async event => {
        event.preventDefault();
        try {
          const response = await api('/usage/export.csv' + currentRange);
          const url = URL.createObjectURL(await response.blob());
          const link = evidenceLink('', url);
          link.download = 'usage.csv';
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) {
          ui.setMessage(feedback, e.message, true);
        }
      };
    }
    const form = ui.form([ui.fields(
      ui.field('开始日期（UTC）', 'from', {type: 'date', value: today.slice(0, 7) + '-01'}),
      ui.field('结束日期（UTC）', 'to', {type: 'date', value: today}),
      ui.field('分组', 'group_by', {choices: [['day', '按天'], ['model', '按模型']]}),
      ...(platform ? [ui.field('租户筛选（可选）', 'tenant_id', {placeholder: '留空查看所有租户'})] : [])
    )], '查询用量', load);
    if (csv) form.querySelector('.form-actions').append(csv);
    root.append(ui.panel(platform ? '所有租户用量' : '用量趋势', form, chart, rows, feedback));

    async function load() {
      const values = new FormData(form), group = values.get('group_by');
      const range = new URLSearchParams();
      for (const name of ['from', 'to']) {
        const value = values.get(name);
        if (value) range.set(name, value);
      }
      const query = new URLSearchParams(range);
      query.set('group_by', group);
      const tenant = values.get('tenant_id')?.trim();
      const loaded = await data(path + '?' + query);
      const filtered = tenant ? loaded.filter(row => row.tenant_id === tenant) : loaded;
      const name = group === 'day' ? '日期（UTC）' : '模型';
      currentRange = range.size ? '?' + range : '';
      if (csv) csv.href = '/v1/usage/export.csv' + currentRange;
      usageChart(chart, filtered, group);
      ui.list(rows, [...(platform ? ['租户'] : []), name, '任务', '轮次', '输入 Token', '输出 Token', '沙箱秒数', '模型费用'],
        filtered.map(row => [...(platform ? [row.tenant_id] : []), row[group], row.tasks, row.turns,
          row.tokens_in, row.tokens_out, Number(row.sandbox_seconds).toFixed(1), ui.cost(row.cost_usd),
        ]), '所选日期范围内无用量。');
    }

    return load;
  }

  function usageChart(target, rows, group) {
    const buckets = new Map();
    for (const row of rows) buckets.set(row[group], (buckets.get(row[group]) || 0) + Number(row.cost_usd));
    const points = [...buckets].sort(([a], [b]) => a.localeCompare(b));
    if (!points.length) {
      target.replaceChildren();
      return;
    }
    const maximum = Math.max(1e-6, ...points.map(([, spent]) => spent));
    const wrap = ui.scroll('模型费用图');
    wrap.classList.add('portal-usage-chart');
    const plot = ui.svg('svg', {viewBox: '0 0 680 ' + (points.length * 26 + 12), role: 'img',
      'aria-label': (group === 'day' ? '按天' : '按模型') + '统计模型费用（美元）', class: 'portal-chart'});
    points.forEach(([label, cost], i) => {
      const y = 8 + 26 * i, width = 380 * cost / maximum;
      const bar = ui.svg('rect', {x: 184, y, width, height: 16, rx: 2, class: 'cost-bar'});
      bar.append(ui.svg('title', {}, label + ' · ' + ui.cost(cost)));
      plot.append(ui.svg('text', {x: 176, y: y + 12, 'text-anchor': 'end'}, label.length > 24 ? label.slice(0, 22) + '…' : label),
        bar, ui.svg('text', {x: 194 + width, y: y + 12}, ui.cost(cost)));
    });
    wrap.append(plot);
    target.replaceChildren(el('h3', '模型费用（美元）'), wrap);
  }

  function quotaBar(label, value, limit, format = n => String(n)) {
    const card = el('div', undefined, 'metric');
    card.append(el('small', label), el('strong', format(value) + (limit == null ? '' : ' / ' + format(limit))));
    if (limit == null) card.append(el('small', '不限额'));
    else {
      const gauge = el('progress');
      gauge.max = Math.max(1, limit);
      gauge.value = Math.min(value, limit);
      gauge.setAttribute('aria-label', label);
      card.append(gauge);
    }
    return card;
  }

  function teamView(root) {
    const info = el('div', undefined, 'stack'), quota = el('div', undefined, 'portal-quota');
    const members = ui.scroll('团队成员'), tokens = ui.scroll('API 令牌'), feedback = message();
    const once = el('div', undefined, 'stack portal-token-once');
    once.hidden = true;
    const clear = () => {
      const input = once.querySelector('input');
      if (input) input.value = '';
      once.replaceChildren();
      once.hidden = true;
    };
    dismissToken = clear;
    const invite = ui.form([ui.fields(ui.field('邀请邮箱', 'email', {type: 'email', required: true, maxLength: 254}),
      ui.field('角色', 'role', {choices: roles}))], '邀请成员', async form => {
      await data(membersPath, {email: ui.value(form, 'email'), role: ui.value(form, 'role')});
      form.reset();
      await loadMembers();
      return '邀请已登记，成员可使用已验证邮箱通过 OIDC 登录。';
    });
    const create = ui.form([ui.fields(
      ui.field('令牌名称', 'name', {required: true, maxLength: 200}),
      ui.field('角色', 'role', {choices: roles}),
      ui.field('有效期（天）', 'days', {type: 'number', required: true, min: 1, max: 3650, value: 90}),
      ui.field('绑定仓库（CI 回报令牌可选）', 'repo_url', {maxLength: 2000}),
      ui.check('仅限 CI 回报', 'ci_scope')
    )], '创建 API 令牌', async form => {
      const identity = portalIdentity;
      clear();
      const ci = ui.checked(form, 'ci_scope');
      const created = await data(tokensPath, {
        name: ui.value(form, 'name'), role: ui.value(form, 'role'), days: Number(ui.value(form, 'days')),
        scopes: ci ? ['ci:report'] : [], repo_url: ci ? ui.value(form, 'repo_url') || null : null,
      });
      if (identity !== portalIdentity || !location.hash.startsWith('#/team') || !created.token) return;
      const field = ui.field('新令牌 · 仅显示一次', 'new_token',
        {readOnly: true, autocomplete: 'off'});
      const input = field.querySelector('input');
      input.value = created.token;
      once.replaceChildren(field, ui.actions(ui.action('复制令牌', async () => {
        const ok = await ui.copy(input.value, input);
        ui.setMessage(feedback, ok ? '已复制 API 令牌。' : '已选中令牌，请使用复制快捷键。');
      }, feedback), ui.action('关闭并清除', clear)));
      once.hidden = false;
      input.focus();
      input.select();
      form.reset();
      await loadTokens();
    });
    root.append(ui.panel('租户资料', info), ui.panel('套餐额度', quota),
      ui.panel('团队成员', members, invite, el('small', '所有者的变更由平台运营处理。')),
      ui.panel('API 令牌', tokens, create, once, feedback));
    const loadBilling = billingView(root);
    const loadUsage = usageView(root);

    async function loadTenant() {
      try {
        const [tenant, limits] = await Promise.all([data('/tenant'), data('/portal/tenant-quota')]);
        const name = ui.form([ui.field('租户名称', 'display_name',
          {required: true, maxLength: 200, value: tenant.display_name})], '更新名称', async form => {
          await data('/tenant', {display_name: ui.value(form, 'display_name')}, 'PATCH');
          return '租户名称已更新。';
        });
        info.replaceChildren(el('p', tenant.id + ' · ' + (tenant.status === 'suspended' ? '已暂停提交' : '正常')
          + ' · 套餐 ' + tenant.plan), el('small', '创建时间 · ' + ui.date(tenant.created_at)), name);
        quota.replaceChildren(
          quotaBar('本月已结算模型费用（UTC）', limits.monthly_cost_usd, limits.monthly_cost_limit_usd, ui.cost),
          quotaBar('已占用运行槽位', limits.slots_used, limits.slots_limit),
          quotaBar('排队任务', limits.queued, limits.queued_limit),
          metric('单任务模型费用上限', limits.max_task_cost_usd == null ? '不限额' : ui.cost(limits.max_task_cost_usd), '')
        );
        invite.hidden = false;
      } catch (e) {
        if (e.status !== 404) throw e;
        info.replaceChildren(el('p', '该租户提交首个任务后会建立租户资料。'));
        quota.replaceChildren();
        invite.hidden = true;
      }
    }
    async function loadMembers() {
      const rows = await data(membersPath);
      const path = row => membersPath + '/' + enc(row.email);
      const roleSelect = row => {
        const select = el('select');
        select.setAttribute('aria-label', row.email + ' 的角色');
        for (const [value, label] of roles) {
          const option = el('option', label);
          option.value = value;
          option.selected = value === row.role;
          select.append(option);
        }
        select.addEventListener('change', async () => {
          try {
            await api(path(row), {role: select.value}, 'PATCH');
            ui.setMessage(feedback, row.email + ' 的角色已更新。');
          } catch (e) {
            select.value = row.role;
            ui.setMessage(feedback, e.message || '角色更新失败。');
          }
        });
        return select;
      };
      ui.list(members, ['邮箱', '角色', '状态', '加入时间', '操作'], rows.map(row => [
        row.email, row.role === 'owner' ? roleLabels.owner : roleSelect(row),
        row.status === 'pending' ? '待首次登录' : '已加入', ui.date(row.activated_at),
        row.role === 'owner' ? '—' : ui.action('移除', async () => {
          if (!confirm('确认将 ' + row.email + ' 移出租户？')) return;
          await api(path(row), undefined, 'DELETE');
          await loadMembers();
        }, feedback),
      ]), '暂无成员邀请。');
    }
    async function loadTokens() {
      const rows = await data(tokensPath);
      ui.list(tokens, ['名称', '角色 / 用途', '有效期', '状态', '操作'], rows.map(row => [
        row.name, row.scopes?.includes('ci:report') ? 'CI 回报' + (row.repo_url ? ' · ' + row.repo_url : '')
          : roleLabels[row.role || 'member'], ui.date(row.expires_at),
        row.revoked_at ? '已吊销' : Date.parse(row.expires_at) <= Date.now() ? '已过期' : '有效',
        row.revoked_at ? '—' : ui.action('吊销', async () => {
          if (!confirm('确认吊销 API 令牌「' + row.name + '」？')) return;
          clear();
          await api(tokensPath + '/' + enc(row.id), undefined, 'DELETE');
          await loadTokens();
        }, feedback),
      ]), '暂无 API 令牌。');
    }
    return () => Promise.all([loadTenant(), loadMembers(), loadTokens(), loadBilling(), loadUsage()]);
  }

  function subscriptionBadge(status) {
    const labels = {
      active: ['订阅有效', 'complete'], trialing: ['试用中', 'active'],
      past_due: ['付款逾期', 'waiting'], unpaid: ['未付款', 'problem'],
      canceled: ['已取消', ''], incomplete: ['待付款', 'waiting'],
      incomplete_expired: ['付款已过期', ''], paused: ['已暂停', 'waiting'],
    };
    const [label, style] = Object.hasOwn(labels, status) ? labels[status] : [status || '未订阅', ''];
    return el('span', label, 'badge ' + style);
  }

  function invoiceAmount(cents, currency) {
    const format = new Intl.NumberFormat('zh-CN', {style: 'currency', currency});
    return format.format(cents / 10 ** format.resolvedOptions().maximumFractionDigits);
  }

  function billingView(root) {
    const summary = el('div', undefined, 'stack'), invoices = ui.scroll('最近发票'), feedback = message();
    root.append(ui.panel('订阅与付款', summary, feedback, invoices));

    async function redirect(path, body) {
      const identity = portalIdentity;
      const result = await data(path, body, 'POST');
      if (identity !== portalIdentity || !location.hash.startsWith('#/team')) return;
      const link = ui.externalLink('Stripe', result.url);
      if (!link.href) throw Error('Stripe 未返回有效的付款链接。');
      location.assign(link.href);
    }

    return async () => {
      try {
        const billing = await data(billingPath);
        const status = el('p', '当前套餐 · ' + billing.plan + ' ');
        status.append(subscriptionBadge(billing.subscription_status));
        summary.replaceChildren(status);
        if (billing.current_period_end) {
          summary.append(el('p', '当前计费周期结束 · ' + ui.date(billing.current_period_end)));
        }
        if (billing.suspended_for_nonpayment) {
          summary.append(el('p', billing.subscription_status === 'canceled'
            ? '订阅已取消，任务提交已暂停。' : '付款逾期，任务提交已暂停。'));
        }
        if (!billing.enabled) summary.append(el('p', 'Stripe 支付尚未配置。'));
        if (billing.enabled && billing.can_checkout && billing.plans.length) {
          const choices = [['', '请选择套餐'], ...billing.plans.map(plan => [plan, plan])];
          summary.append(ui.form([ui.field('订阅套餐', 'plan', {choices, required: true})], '前往结账',
            form => redirect('/tenant/billing/checkout', {plan: ui.value(form, 'plan')})));
        }
        if (billing.enabled && billing.has_customer) {
          summary.append(ui.actions(ui.action('管理付款方式', () => redirect('/tenant/billing/portal'), feedback)));
        }
        const labels = {draft: '草稿', open: '待付款', paid: '已付款', uncollectible: '无法收款', void: '已作废'};
        ui.list(invoices, ['发票', '状态', '应付', '实付', '账期结束'], billing.invoices.map(invoice => [
          ui.externalLink(invoice.number || invoice.id, invoice.hosted_invoice_url),
          Object.hasOwn(labels, invoice.status) ? labels[invoice.status] : invoice.status,
          invoiceAmount(invoice.amount_due, invoice.currency), invoiceAmount(invoice.amount_paid, invoice.currency),
          ui.date(invoice.period_end),
        ]), '暂无发票。');
      } catch (e) {
        if (e.status !== 404) throw e;
        summary.replaceChildren(el('p', '租户建立后可查看付款信息。'));
        invoices.replaceChildren();
      }
    };
  }

  function platformView(root) {
    const tenants = ui.scroll('平台租户'), planList = ui.scroll('平台套餐'), editor = el('div'), feedback = message();
    root.append(ui.panel('平台租户', tenants, feedback), ui.panel('创建租户', editor), ui.panel('套餐', planList));
    const loadUsage = usageView(root, true);
    let planSignature = '', choices = [];
    async function loadTenants() {
      const [list, plans, billing] = await Promise.all([
        data(tenantsPath), data('/admin/plans'), data('/admin/billing'),
      ]);
      const subscriptions = new Map(billing.map(row => [row.tenant_id, row.subscription_status]));
      choices = plans.map(plan => [plan.name, plan.name]);
      ui.list(planList, ['套餐', '月度模型费用上限', '并发任务上限', '单任务模型费用上限'], plans.map(plan => [
        plan.name, plan.monthly_cost_limit_usd == null ? '不限额' : ui.cost(plan.monthly_cost_limit_usd),
        plan.max_concurrent_tasks ?? '沿用租户上限', plan.max_task_cost_usd == null ? '不限额' : ui.cost(plan.max_task_cost_usd),
      ]));
      if (JSON.stringify(choices) !== planSignature) {
        planSignature = JSON.stringify(choices);
        editor.replaceChildren(ui.form([ui.fields(
          ui.field('租户 ID', 'id', {required: true, pattern: '[a-z0-9][a-z0-9\\-]{0,62}'}),
          ui.field('显示名称', 'display_name', {required: true, maxLength: 200}),
          ui.field('套餐', 'plan', {choices, value: choices.some(([key]) => key === 'free') ? 'free' : choices[0]?.[0]}),
          ui.field('所有者邮箱（可选）', 'owner_email', {type: 'email', maxLength: 254})
        )], '创建租户', async form => {
          const v = name => ui.value(form, name);
          await data(tenantsPath, {id: v('id'), display_name: v('display_name'), plan: v('plan'),
            owner_email: v('owner_email') || null});
          form.reset();
          await loadTenants();
          return '租户已创建。';
        }));
      }
      ui.list(tenants, ['租户', '创建时间', '状态', '套餐', '订阅状态', '操作'], list.map(row => {
        const path = tenantsPath + '/' + enc(row.id), suspended = row.status === 'suspended';
        const plan = ui.form([ui.field('当前套餐', 'plan', {choices, value: row.plan})], '保存套餐', async form => {
          await data(path, {plan: ui.value(form, 'plan')}, 'PATCH');
          return '套餐已更新。';
        });
        return [row.display_name + ' · ' + row.id, ui.date(row.created_at),
          suspended ? '已暂停提交' : '正常', plan, subscriptionBadge(subscriptions.get(row.id)),
          ui.action(suspended ? '恢复提交' : '暂停提交', async () => {
            if (!confirm((suspended ? '恢复' : '暂停') + '租户 ' + row.id + ' 的任务提交？')) return;
            await data(path + (suspended ? '/resume' : '/suspend'), {});
            await loadTenants();
          }, feedback),
        ];
      }), '尚无租户。');
    }
    return () => Promise.all([loadTenants(), loadUsage()]);
  }

  function load(page) {
    let view = mounted.get(page);
    if (!view) {
      const root = $(page + '-content');
      const mount = {tools: toolsView, integrations: integrationsView, team: teamView, platform: platformView}[page];
      view = {load: mount(root)};
      mounted.set(page, view);
    }
    if (!view.pending) view.pending = view.load().finally(() => { view.pending = null; });
    return view.pending;
  }

  function configureAccess(required) {
    const visible = typeof required === 'boolean';
    $('portal-access-control').hidden = !visible;
    const input = $('portal-require-token');
    if (visible && !input.disabled) input.checked = required;
  }

  function bindAccess() {
    const input = $('portal-require-token');
    input.onchange = async () => {
      const identity = portalIdentity, required = input.checked;
      if (required && identity?.local_no_token && !confirm(
        '开启后需用 .data/portal.env 中的 SCA_EVAL_API_TOKEN 登录。请在本机终端私下查看。\n\n'
        + 'PowerShell\nSelect-String SCA_EVAL_API_TOKEN .\\.data\\portal.env\n\n'
        + 'Bash\ngrep SCA_EVAL_API_TOKEN .data/portal.env'
      )) {
        input.checked = false;
        return;
      }
      input.disabled = true;
      try {
        const status = await data('/portal/access', {require_token: required}, 'PUT');
        if (identity !== portalIdentity) return;
        input.checked = status.require_token;
        if (status.require_token && identity?.local_no_token) {
          resetPortalSession();
          displayConnection('请使用访问令牌连接。', '');
          syncPortalLogin();
          await route();
          await refresh(true);
          $('token').focus();
        }
      } catch (e) {
        if (identity === portalIdentity) input.checked = !required;
        error(e);
      } finally {
        input.disabled = false;
      }
    };
  }

  function clearSensitive() {
    dismissToken?.();
  }

  function reset() {
    configureAccess();
    clearSensitive();
    dismissToken = null;
    for (const page of mounted.keys()) $(page + '-content').replaceChildren();
    mounted.clear();
  }

  return {load, reset, clearSensitive, configureAccess, bindAccess};
})();
