'use strict';

const portalModels = (() => {
  const ui = portalUI, path = '/portal/providers';
  const types = [['openai-compatible', 'OpenAI 兼容'], ['anthropic-compatible', 'Anthropic 兼容'], ['deepseek', 'DeepSeek']];
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

  async function request(endpoint, body, method) {
    let response;
    try {
      response = await fetch('/v1' + endpoint, {
        method, headers: {...authHeaders(), 'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(130000),
      });
    } catch {
      throw Error('模型接入请求失败，请检查 Portal 和网关状态。');
    }
    if (!response.ok) throw await apiFailure(response);
    return response.json();
  }

  function tester(models, message) {
    const row = el('div', undefined, 'input-row');
    const choice = ui.field('测试模型', 'test_model', {choices: models.map(name => [name, name])});
    const control = choice.querySelector('select');
    const test = ui.action('测试连接', async () => {
      ui.setMessage(message, '正在通过网关测试连接…');
      const result = await request(path + '/test', {model: control.value}, 'POST');
      ui.setMessage(message, result.message + ' · ' + result.latency_ms + ' 毫秒', !result.ok);
    }, message);
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

  function changed(state, result) {
    if (view !== state) return;
    ui.setMessage(state.message, result.gateway_restarted ? '已保存，模型网关已重启。'
      : '已保存，网关重启失败。请检查网关日志后重启。', !result.gateway_restarted);
    edit(state);
    return Promise.all([loadConfig(), refresh(state)]);
  }

  function edit(state, provider = null) {
    const list = el('div', undefined, 'stack');
    for (const model of provider?.models || [{}]) modelFields(list, model);
    const hint = portalConfig?.model_docker_mode
      ? 'Docker 网关会把 localhost、127.0.0.1 和 ::1 转为 host.docker.internal。Linux 上请让服务监听网桥可达的地址。'
      : '源码模式可用本机地址，如 http://127.0.0.1:11434/v1。';
    const form = ui.form([
      el('h3', provider ? '编辑供应商' : '添加供应商'),
      ui.fields(
        ui.field('名称', 'name', {value: provider?.name || '', maxLength: 100, required: true}),
        ui.field('接口类型', 'type', {choices: types, value: provider?.type || 'openai-compatible'}),
        ui.field('基础 URL', 'base_url', {type: 'url', value: provider?.base_url || '', required: true, wide: true, note: hint}),
        ui.field('API Key（可选）', 'api_key', {type: 'password', autocomplete: 'new-password', maxLength: 4096,
          note: provider?.has_key ? '留空保留密钥。修改 URL 或类型时请重新填写或清除。' : '免密服务可留空。'}),
        ...(provider?.has_key ? [ui.check('清除现有密钥', 'clear_key')] : []),
      ), list, el('small', '价格留空的模型按 $0 计费，美元预算不会限制其用量。'),
      ui.actions(ui.action('添加模型', () => modelFields(list)), provider ? ui.action('取消编辑', () => edit(state)) : null),
    ], '保存并重启网关', async form => {
      const key = ui.control(form, 'api_key');
      const body = {name: ui.value(form, 'name'), type: ui.value(form, 'type'),
        base_url: ui.value(form, 'base_url'), models: [...list.children].map(readModel)};
      if (ui.control(form, 'clear_key')?.checked) body.api_key = null;
      else if (key.value) body.api_key = key.value;
      key.value = '';
      ui.setMessage(state.message, '正在保存并重启模型网关…');
      let result;
      try {
        result = await request(provider ? path + '/' + enc(provider.id) : path, body, provider ? 'PUT' : 'POST');
      } catch (e) {
        ui.setMessage(state.message, e.message, true);
        return;
      }
      await changed(state, result);
    });
    state.editor.replaceChildren(form);
  }

  async function refresh(state = view) {
    if (!state) return;
    try {
      const config = await data(path);
      if (view !== state) return;
      state.preset.replaceChildren(el('p', config.deepseek_models.length ? '内置 DeepSeek 已接入。'
        : '可在上方输入 DeepSeek API Key 启用内置模型。'),
        ...(config.deepseek_models.length ? [tester(config.deepseek_models, state.message)] : []));
      ui.list(state.list, ['供应商', '基础 URL', '模型', '操作'], config.providers.map(provider => [
        provider.name + ' · ' + types.find(([type]) => type === provider.type)?.[1], provider.base_url,
        provider.models.map(model => model.name).join('、'),
        ui.actions(ui.action('编辑', () => edit(state, provider)),
          ui.action('删除', async () => {
            if (!confirm('删除供应商「' + provider.name + '」？')) return;
            await changed(state, await request(path + '/' + enc(provider.id), undefined, 'DELETE'));
          }, state.message),
          tester(provider.models.map(model => model.name), state.message)),
      ]), '尚未添加自定义供应商。');
    } catch (e) {
      if (view === state) ui.setMessage(state.message, e.message, true);
    }
  }

  function reset() {
    const root = $('model-providers');
    root.hidden = true;
    root.replaceChildren();
    view = null;
  }

  function modelOptions() {
    options('models', portalConfig?.models || [], name => name, name => unpriced(name) ? name + '（未定价）' : name);
  }

  function setCatalog(catalog) {
    if (!portalConfig || !Array.isArray(catalog?.data)) return;
    Object.assign(portalConfig, {
      default_model: catalog.default_model,
      evolve_model: catalog.evolve_model,
      models: catalog.data.map(model => model.id),
      unpriced_models: catalog.data.filter(model => model.unpriced).map(model => model.id),
    });
    modelOptions();
  }

  function configure(config, previous) {
    for (const id of ['model', 'fix-model']) {
      const input = $(id);
      if (!input.value || (input.value === previous && !config.models?.includes(previous))) {
        input.value = config.default_model || '';
      }
    }
    modelOptions();
    if (!config.can_configure_model) {
      reset();
      return;
    }
    if (view) return;
    const root = $('model-providers'), message = el('p', undefined, 'status-text portal-message');
    message.setAttribute('role', 'status');
    view = {list: ui.scroll('模型供应商'), editor: el('div'), preset: el('div'), message};
    root.replaceChildren(el('h2', '模型接入'), view.preset, view.list, view.message, view.editor);
    root.hidden = false;
    edit(view);
    void refresh();
  }

  return {bindBudget, budgetWarning, configure, field, multipleAliases, refresh, reset, setCatalog};
})();
