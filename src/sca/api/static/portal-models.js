'use strict';

const portalModels = (() => {
  const ui = portalUI, path = '/portal/providers';
  const types = [['openai-compatible', 'OpenAI 兼容'], ['anthropic-compatible', 'Anthropic 兼容'], ['deepseek', 'DeepSeek']];
  const presets = [
    {name: 'OpenAI 兼容', type: 'openai-compatible', base_url: 'https://api.openai.com/v1'},
    {name: 'Anthropic 兼容', type: 'anthropic-compatible', base_url: 'https://api.anthropic.com'},
  ];
  const localPresets = [
    {name: 'Ollama', type: 'openai-compatible', base_url: 'http://127.0.0.1:11434/v1'},
    {name: 'vLLM', type: 'openai-compatible', base_url: 'http://127.0.0.1:8001/v1'},
    {name: 'LM Studio', type: 'openai-compatible', base_url: 'http://127.0.0.1:1234/v1'},
  ];
  const deepseekModels = ['deepseek-flash', 'deepseek-v4-pro'];
  let view = null;

  function field(label, name, properties = {}) {
    const wrap = ui.field(label, name, properties);
    wrap.querySelector('input').setAttribute('list', 'models');
    return wrap;
  }

  function unpriced(name) {
    return portalConfig?.unpriced_models?.includes(name) === true;
  }

  function budgetWarning(names) {
    return names.some(unpriced) ? '未定价模型按 $0 计费，美元预算不会限制其用量。' : '';
  }

  function bindBudget(form, selected) {
    const note = el('small', '', 'status-text');
    note.setAttribute('role', 'status');
    form.append(note);
    const update = () => {
      note.textContent = budgetWarning(selected(form));
      note.hidden = !note.textContent;
    };
    form.addEventListener('change', update);
    form.addEventListener('input', update);
    form.addEventListener('reset', () => queueMicrotask(update));
    update();
  }

  function multipleAliases(input, list) {
    input.setAttribute('list', list.id);
    const update = () => {
      const prefix = input.value.match(/^.*[,，]\s*/u)?.[0] || '';
      list.replaceChildren(...(portalConfig?.models || []).map(name => {
        const option = el('option');
        option.value = prefix + name;
        return option;
      }));
    };
    input.addEventListener('input', update);
    input.addEventListener('focus', update);
  }

  async function request(endpoint, body, method = 'GET', timeout = 15000) {
    let response;
    try {
      response = await fetch('/v1' + endpoint, {
        method, headers: {...authHeaders(), 'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(timeout),
      });
    } catch {
      throw Error('模型接入请求失败，请检查 Portal 和网关状态。');
    }
    if (!response.ok) throw await apiFailure(response, true);
    return response.json();
  }

  function tester(models, message, enabled) {
    const row = el('div', undefined, 'input-row');
    const choice = ui.field('测试模型', 'test_model', {choices: models.map(name => [name, name])});
    const control = choice.querySelector('select');
    const test = ui.action('测试连接', async () => {
      ui.setMessage(message, '正在通过网关测试连接…');
      const result = await request(path + '/test', {model: control.value}, 'POST', 130000);
      ui.setMessage(message, result.message + ' · ' + result.latency_ms + ' 毫秒', !result.ok);
    }, message);
    control.disabled = test.disabled = !enabled;
    row.append(choice, test);
    return row;
  }

  function modelFields(list, model = {}) {
    const inputs = ui.fields(
      ui.field('平台模型名', 'route_name', {value: model.name || '', maxLength: 200, required: true}),
      ui.field('上游模型 ID', 'upstream_model', {value: model.upstream_model || '', maxLength: 200, required: true}),
      ui.field('上下文窗口（Token）', 'context_window', {type: 'number', value: model.context_window ?? '', min: 1}),
      ui.field('输入价格（美元 / 百万 Token）', 'input_usd_per_million', {
        type: 'number', value: model.input_usd_per_million ?? '', min: 0, step: 'any',
      }),
      ui.field('输出价格（美元 / 百万 Token）', 'output_usd_per_million', {
        type: 'number', value: model.output_usd_per_million ?? '', min: 0, step: 'any',
      })
    );
    const group = el('fieldset', undefined, 'stack');
    group.append(el('legend', '模型'), inputs, ui.actions(ui.action('移除模型', () => {
      if (list.children.length > 1) group.remove();
    })));
    list.append(group);
    return group;
  }

  function readModel(row) {
    const value = name => row.querySelector('[name="' + name + '"]').value.trim();
    const number = name => value(name) === '' ? null : Number(value(name));
    return {name: value('route_name'), upstream_model: value('upstream_model'), context_window: number('context_window'),
      input_usd_per_million: number('input_usd_per_million'), output_usd_per_million: number('output_usd_per_million')};
  }

  async function waitForRestart(state, restart) {
    const deadline = Date.now() + 135000;
    while (view === state && restart.status === 'restarting') {
      if (Date.now() >= deadline) throw Error('查询网关重启结果超时。');
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (view !== state) break;
      restart = await request(path + '/restarts/' + enc(restart.id));
    }
    return restart.status === 'succeeded';
  }

  async function change(state, endpoint, body, method, {legacy = false, defaultModel = false} = {}) {
    if (view !== state || state.busy) return;
    state.busy = true;
    state.controls.inert = true;
    ui.setMessage(state.message, legacy ? '正在保存并重启模型网关…' : '正在保存模型接入配置…');
    let saved = false;
    try {
      const result = await request(endpoint, body, method, legacy ? 130000 : undefined);
      saved = true;
      if (view !== state) return;
      let succeeded = result.gateway_restarted;
      if (result.restart) {
        ui.setMessage(state.message, '配置已保存，正在重启模型网关…');
        succeeded = await waitForRestart(state, result.restart);
      }
      if (view !== state) return;
      ui.setMessage(state.message, defaultModel ? '默认模型已保存。' : succeeded
        ? '配置已保存，模型网关已重启。' : '配置已保存，网关重启失败。请检查网关日志后重启。',
        !defaultModel && !succeeded);
    } catch (e) {
      if (view === state) ui.setMessage(state.message, saved
        ? '配置已保存，无法确认网关重启结果。' + e.message : e.message, true);
    } finally {
      if (saved && view === state) {
        state.editor.hidden = true;
        state.editor.replaceChildren();
        try {
          await loadConfig();
          if (view === state) await refresh(state);
        } catch (e) {
          if (view === state) ui.setMessage(state.message, '配置已保存，刷新失败。' + e.message, true);
        }
      }
      state.busy = false;
      state.controls.inert = false;
    }
  }

  function edit(state, provider) {
    const list = el('div', undefined, 'stack');
    for (const model of provider.models || [{}]) modelFields(list, model);
    const hint = portalConfig?.model_docker_mode
      ? 'Docker 网关会把 localhost、127.0.0.1 和 ::1 转为 host.docker.internal。Linux 上请让服务监听网桥可达的地址。'
      : '源码模式可用本机地址，如 http://127.0.0.1:11434/v1。';
    const form = ui.form([
      el('h2', provider.id ? '编辑供应商' : '添加供应商'),
      ui.fields(
        ui.field('名称', 'name', {value: provider.name, maxLength: 100, required: true}),
        ui.field('接口类型', 'type', {choices: types, value: provider.type}),
        ui.field('基础 URL', 'base_url', {type: 'url', value: provider.base_url, required: true, wide: true, note: hint}),
        ui.field('API Key（可选）', 'api_key', {type: 'password', autocomplete: 'new-password', maxLength: 4096,
          placeholder: provider.has_key ? '已配置' : '',
          note: provider.has_key ? '留空保留密钥。修改 URL 或类型时请重新填写或清除。' : '免密服务可留空。'}),
        ...(provider.has_key ? [ui.check('清除现有密钥', 'clear_key')] : []),
      ), list, el('small', '价格留空的模型按 $0 计费，美元预算不会限制其用量。'),
      ui.actions(ui.action('添加模型', () => modelFields(list)), ui.action('取消编辑', () => {
        state.editor.hidden = true;
        state.editor.replaceChildren();
      })),
    ], '保存并重启网关', async form => {
      const key = ui.control(form, 'api_key');
      const body = {name: ui.value(form, 'name'), type: ui.value(form, 'type'),
        base_url: ui.value(form, 'base_url'), models: [...list.children].map(readModel)};
      if (ui.control(form, 'clear_key')?.checked) body.api_key = null;
      else if (key.value) body.api_key = key.value;
      key.value = '';
      await change(state, provider.id ? path + '/' + enc(provider.id) : path, body, provider.id ? 'PUT' : 'POST');
    });
    state.editor.hidden = false;
    state.editor.replaceChildren(form);
    ui.control(form, 'name').focus();
  }

  function modelList(models) {
    const list = el('div', undefined, 'provider-models');
    list.append(...(models.length ? models.map(name => el('code', name)) : [el('span', '暂无模型')]));
    return list;
  }

  function card(title, enabled, ...content) {
    const root = el('article', undefined, 'panel provider-card');
    const head = el('div', undefined, 'panel-head');
    head.append(el('h3', title));
    if (enabled !== null) head.append(el('span', enabled ? '已启用' : '已停用', 'badge ' + (enabled ? 'complete' : '')));
    root.append(head, ...content);
    return root;
  }

  function details(...rows) {
    const list = el('dl');
    for (const [name, value] of rows) {
      const row = el('div'), description = el('dd');
      description.append(value);
      row.append(el('dt', name), description);
      list.append(row);
    }
    return list;
  }

  function toggle(state, endpoint, enabled) {
    return ui.action(enabled ? '停用' : '启用', () => change(state, endpoint, {enabled: !enabled}, 'PUT'), state.message);
  }

  function deepseekCard(state, config) {
    const hasKey = config.deepseek_has_key;
    const enabled = hasKey && config.deepseek_enabled;
    const models = config.deepseek_models.length ? config.deepseek_models : deepseekModels;
    const endpoint = '/portal/provider-key';
    const form = ui.form([
      ui.field('基础 URL', 'base_url', {type: 'url', value: 'https://api.deepseek.com', readOnly: true}),
      ui.field('API Key', 'key', {type: 'password', autocomplete: 'new-password', maxLength: 256,
        required: true, placeholder: hasKey ? '已配置' : '',
        note: hasKey ? '已配置。填写新密钥可替换。' : '填写密钥并保存以接入 DeepSeek。'}),
    ], hasKey ? '更新密钥' : '保存密钥', async form => {
      const body = {key: ui.value(form, 'key')};
      ui.control(form, 'key').value = '';
      await change(state, endpoint, body, 'POST', {legacy: true});
    });
    const actions = hasKey ? ui.actions(toggle(state, path + '/deepseek/enabled', enabled),
      ui.action('删除密钥', async () => {
        if (confirm('删除 DeepSeek 的密钥并移除内置模型？')) {
          await change(state, endpoint, undefined, 'DELETE', {legacy: true});
        }
      }, state.message)) : null;
    return card('DeepSeek', hasKey ? enabled : null, form,
      details(['模型', modelList(models)]), tester(models, state.message, enabled),
      ...(actions ? [actions] : []));
  }

  function providerCard(state, provider) {
    const models = provider.models.map(model => model.name);
    const endpoint = path + '/' + enc(provider.id);
    return card(provider.name, provider.enabled,
      details(['接口类型', types.find(([type]) => type === provider.type)?.[1] || provider.type],
        ['基础 URL', provider.base_url], ['API Key', provider.has_key ? '已配置' : '免密'], ['模型', modelList(models)]),
      tester(models, state.message, provider.enabled),
      ui.actions(ui.action('编辑', () => edit(state, provider)), toggle(state, endpoint + '/enabled', provider.enabled),
        ui.action('删除', async () => {
          if (confirm('删除供应商「' + provider.name + '」？')) await change(state, endpoint, undefined, 'DELETE');
        }, state.message)));
  }

  function presetCard(state, preset) {
    return card(preset.name, null, details(['基础 URL', preset.base_url]),
      ui.actions(ui.action('添加供应商', () => edit(state, preset))));
  }

  function localCard(state) {
    const pick = ui.field('本地服务', 'local_preset', {choices: localPresets.map((preset, i) => [i, preset.name])});
    const choice = pick.querySelector('select');
    return card('本地模型', null, pick, ui.actions(
      ui.action('添加供应商', () => edit(state, localPresets[Number(choice.value)]))));
  }

  function defaultPanel(state, config, catalog) {
    const choices = catalog.data.map(({id}) => [id, id]);
    if (config.default_model && !choices.some(([id]) => id === config.default_model)) {
      choices.unshift([config.default_model, config.default_model + '（暂不可用）']);
    }
    const form = ui.form([
      ui.field('默认模型', 'model', {
        choices: [['', '跟随服务配置'], ...choices], value: config.default_model || '',
        note: catalog.data.length ? '当前默认模型：' + catalog.default_model : '接入模型后即可选择默认模型。',
      }),
    ], '保存默认模型', form => change(state, path + '/default', {model: ui.value(form, 'model') || null}, 'PUT',
      {defaultModel: true}));
    return ui.panel('模型默认值', form);
  }

  async function refresh(state) {
    const [catalog, config] = await Promise.all([request('/models'), state.manage ? request(path) : null]);
    if (view !== state) return;
    setCatalog(catalog);
    if (!state.manage) {
      state.contents.replaceChildren(ui.panel('可用模型', modelList(catalog.data.map(model => model.id)),
        el('p', '默认模型：' + (catalog.data.length ? catalog.default_model : '—')),
        el('small', '使用本机 eval 管理员身份可管理供应商。')));
      return;
    }
    const cards = el('div', undefined, 'provider-grid');
    cards.append(deepseekCard(state, config), ...presets.map(preset => presetCard(state, preset)), localCard(state));
    const providers = el('div', undefined, 'provider-grid');
    providers.append(...config.providers.map(provider => providerCard(state, provider)));
    state.contents.replaceChildren(defaultPanel(state, config, catalog), cards, ...(config.providers.length
      ? [el('h2', '已添加的供应商'), providers] : []));
  }

  async function load() {
    const manage = ui.can('eval-admin') && portalConfig?.can_configure_model === true;
    if (!view || view.manage !== manage) {
      const message = el('p', undefined, 'status-text portal-message');
      message.setAttribute('role', 'status');
      const contents = el('div', undefined, 'page'), controls = el('div', undefined, 'page');
      const editor = el('section', undefined, 'panel provider-editor');
      editor.hidden = true;
      contents.append(el('p', '正在读取模型配置…'));
      controls.append(contents, editor);
      $('model-providers').replaceChildren(message, controls);
      view = {manage, contents, controls, editor, message};
    }
    const state = view;
    if (state.busy) return;
    try {
      await refresh(state);
    } catch (e) {
      if (view === state) ui.setMessage(state.message, e.message, true);
    }
  }

  function clearSensitive() {
    for (const input of $('model-providers').querySelectorAll('input[type="password"]')) input.value = '';
  }

  function reset() {
    clearSensitive();
    $('model-providers').replaceChildren();
    view = null;
  }

  function modelOptions() {
    options('models', portalConfig?.models || [], name => name, name => unpriced(name) ? name + '（未定价）' : name);
  }

  function setCatalog(catalog) {
    if (!portalConfig || !Array.isArray(catalog?.data)) return;
    const previous = portalConfig.default_model;
    Object.assign(portalConfig, {
      default_model: catalog.default_model,
      evolve_model: catalog.evolve_model,
      models: catalog.data.map(model => model.id),
      unpriced_models: catalog.data.filter(model => model.unpriced).map(model => model.id),
    });
    configure(portalConfig, previous);
  }

  function configure(config, previous) {
    for (const id of ['model', 'fix-model']) {
      const input = $(id);
      if (!input.value || input.value === previous) {
        input.value = config.default_model || '';
      }
    }
    modelOptions();
  }

  return {bindBudget, budgetWarning, clearSensitive, configure, field, load, multipleAliases, reset, setCatalog};
})();
