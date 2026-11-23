'use strict';

window.SCAPortalRuntime = (() => {
  const endpoint = '/v1/portal/runtime';
  const labels = {source: '源码模式', docker: 'Docker 模式'};
  const descriptions = {
    source: '无容器隔离，适合 Windows 或未安装 Docker 的环境。',
    docker: '任务在沙箱内隔离运行，需要 Docker。',
  };
  const steps = {
    queued: '等待切换', export: '导出历史记录', stop: '停止原运行模式', start: '启动目标模式',
    import: '导入历史记录', health: '检查服务健康状态', rollback: '恢复原运行模式', done: '切换完成',
  };
  const mounts = new WeakMap();

  function node(tag, text = '', className = '') {
    const element = document.createElement(tag);
    element.textContent = text;
    element.className = className;
    return element;
  }

  async function request(body, timeout = 30000) {
    const response = await fetch(endpoint + (body ? '/switch' : ''), {
      method: body ? 'POST' : 'GET', cache: 'no-store', signal: AbortSignal.timeout(timeout),
      headers: {...window.authHeaders?.(), ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined,
    });
    const result = await response.json();
    if (!response.ok) {
      throw Object.assign(Error(result.error?.message || '运行模式请求失败。'), {status: response.status});
    }
    return result;
  }

  function mount(target) {
    if (mounts.has(target)) return mounts.get(target)();
    const current = node('p');
    const choices = node('div', '', 'cards');
    const status = node('p', '', 'status-text');
    status.setAttribute('role', 'status');
    const log = node('pre');
    log.hidden = true;
    target.replaceChildren(node('h2', '运行模式'), current, choices,
      node('small', '切换时自动迁移历史记录'), status, log);

    let info = null, loading = false, switching = false;
    const mounted = () => mounts.get(target) === load;

    function render() {
      const record = info.switch;
      const preferred = info.preferred_mode && info.preferred_mode !== info.current_mode
        ? ' · 首选启动模式 ' + labels[info.preferred_mode] : '';
      current.textContent = '当前运行模式 ' + labels[info.current_mode] + preferred;
      choices.replaceChildren(...info.modes.map(({mode, available, reason}) => {
        const isCurrent = mode === info.current_mode;
        const card = node('div', '', isCurrent ? 'feature runtime-current' : 'feature');
        const heading = node('div', '', 'runtime-mode-heading');
        heading.append(node('h3', labels[mode]));
        if (isCurrent) heading.append(node('span', '当前', 'badge active'));
        const choice = node('button', isCurrent ? '当前模式' : '切换到 ' + labels[mode]);
        choice.type = 'button';
        choice.disabled = isCurrent || !available || switching || record?.status === 'running';
        choice.onclick = () => switchMode(mode);
        const detail = node('div');
        detail.append(node('p', descriptions[mode]));
        if (!available) detail.append(node('small', reason));
        card.append(heading, detail, choice);
        return card;
      }));
      status.textContent = record?.status === 'failed' ? record.error
        : record?.status === 'succeeded' ? '运行模式切换完成。'
          : record ? '正在切换…（' + steps[record.step] + '）' : '';
      log.textContent = record?.log_tail || '';
      log.hidden = !log.textContent;
      target.hidden = false;
    }

    async function watch(id, step) {
      const deadline = Date.now() + 600000;
      let delay = 1000;
      while (mounted() && Date.now() < deadline) {
        try {
          const health = await fetch('/healthz', {
            cache: 'no-store', signal: AbortSignal.timeout(Math.min(3000, deadline - Date.now())),
          });
          if (!mounted()) return;
          if (!health.ok || Date.now() >= deadline) throw Error();
          const snapshot = await request(undefined, Math.min(30000, deadline - Date.now()));
          if (!mounted()) return;
          if (snapshot.switch?.id === id) {
            info = snapshot;
            step = info.switch.step;
            switching = info.switch.status === 'running';
            render();
            if (!switching) {
              // Reload workspace state and the source-mode banner from the running API.
              window.location.reload();
              return;
            }
          }
        } catch {
          if (!mounted()) return;
          status.textContent = '正在重启…（' + steps[step] + '）';
        }
        const remaining = deadline - Date.now();
        if (remaining > 0) await new Promise(resolve => setTimeout(resolve, Math.min(delay, remaining)));
        delay = Math.min(8000, delay * 2);
      }
      if (!mounted()) return;
      switching = false;
      status.textContent = '等待 Portal 恢复超时。请刷新数据查看最新切换结果。';
    }

    async function switchMode(mode) {
      if (switching || !window.confirm('切换到 ' + labels[mode] + ' 并重启 Portal？切换时自动迁移历史记录。')) return;
      switching = true;
      render();
      status.textContent = '正在提交切换请求…';
      let accepted;
      try {
        accepted = await request({mode, carry_history: true});
      } catch (error) {
        if (!mounted()) return;
        switching = false;
        render();
        status.textContent = error.status ? error.message : '无法提交切换请求，请刷新数据查看切换状态。';
        return;
      }
      if (!mounted()) return;
      status.textContent = '正在切换…（等待切换）';
      await watch(accepted.id, 'queued');
    }

    async function load() {
      if (loading || switching) return;
      loading = true;
      try {
        const snapshot = await request();
        if (!mounted()) return;
        info = snapshot;
        switching = info.switch?.status === 'running';
        render();
        if (switching) void watch(info.switch.id, info.switch.step);
      } catch (error) {
        if (!mounted()) return;
        target.hidden = error.status === 401 || error.status === 403;
        if (!target.hidden) status.textContent = '无法读取运行模式，请刷新数据后重试。';
      } finally {
        loading = false;
      }
    }

    mounts.set(target, load);
    return load();
  }

  function unmount(target) {
    mounts.delete(target);
    target.replaceChildren();
    target.hidden = false;
  }

  return {mount, unmount};
})();
