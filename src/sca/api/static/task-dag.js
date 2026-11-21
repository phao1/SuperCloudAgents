'use strict';

const SCATaskDAG = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const width = 178, height = 60, columnGap = 84, rowGap = 26, margin = 24;
  const statusLabels = {
    pending: '待执行', running: '运行中', succeeded: '已完成',
    failed: '失败', cancelled: '已取消', skipped: '已跳过',
  };
  const kindLabels = {
    task: '任务', plan: '规划', subtask: '子任务', attempt: '候选', turn: 'Agent 轮次',
    tool_group: '工具', verification: '验证', fix: '修复轮次', interjection: '插话',
    merge: '汇合', review: '人工审核', pr: 'PR', ci: 'CI', finish: '结束',
  };
  const edgeKinds = ['sequence', 'dependency', 'spawn', 'select', 'merge'];

  function layout(dag) {
    const nodes = new Map(dag.nodes.map(node => [node.id, {
      node, incoming: [], outgoing: [], remaining: 0, rank: 0, y: 0,
    }]));
    const edges = [];
    for (const edge of dag.edges) {
      const source = nodes.get(edge.from), target = nodes.get(edge.to);
      if (!source || !target) continue;
      source.outgoing.push(target);
      target.incoming.push(source);
      target.remaining++;
      edges.push({source, target, kind: edge.kind});
    }
    const ready = [...nodes.values()].filter(node => !node.remaining), columns = [];
    for (let index = 0; index < ready.length; index++) {
      const current = ready[index];
      (columns[current.rank] ??= []).push(current);
      for (const target of current.outgoing) {
        target.rank = Math.max(target.rank, current.rank + 1);
        if (--target.remaining === 0) ready.push(target);
      }
    }
    const rows = Math.max(1, ...columns.map(column => column.length));
    for (const [rank, column] of columns.entries()) {
      for (const item of column) {
        item.order = item.incoming.length
          ? item.incoming.reduce((sum, from) => sum + from.y, 0) / item.incoming.length : 0;
      }
      column.sort((a, b) => a.order - b.order);
      const top = margin + (rows - column.length) * (height + rowGap) / 2;
      column.forEach((item, index) => {
        item.x = margin + rank * (width + columnGap);
        item.y = top + index * (height + rowGap);
      });
    }
    return {
      nodes: ready, edges: edges.filter(edge => edge.source.x !== undefined && edge.target.x !== undefined),
      width: margin * 2 + Math.max(1, columns.length) * (width + columnGap) - columnGap,
      height: margin * 2 + rows * (height + rowGap) - rowGap,
    };
  }

  function svgElement(tag, attrs = {}, label) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (label !== undefined) node.textContent = label;
    return node;
  }

  function element(tag, label, className) {
    const node = document.createElement(tag);
    if (label !== undefined) node.textContent = label;
    if (className) node.className = className;
    return node;
  }

  function edgePath({source, target}) {
    const x1 = source.x + width, y1 = source.y + height / 2;
    const x2 = target.x, y2 = target.y + height / 2;
    const curve = Math.min((x2 - x1) / 2, 132);
    return `M ${x1} ${y1} C ${x1 + curve} ${y1}, ${x2 - curve} ${y2}, ${x2} ${y2}`;
  }

  function describeTime(value) {
    const time = value ? new Date(value) : null;
    return time && Number.isFinite(time.valueOf()) ? time.toLocaleString('zh-CN') : null;
  }

  function appendTools(detail, meta, open) {
    if (!meta.tools?.length) return;
    const list = element('ol');
    for (const tool of meta.tools) {
      const item = element('li');
      const time = Number.isFinite(tool.duration_ms) ? ' · ' + (tool.duration_ms / 1000).toFixed(2) + ' 秒' : '';
      const code = tool.exit_code == null ? '' : ' · 退出码 ' + tool.exit_code;
      item.append(element('strong', tool.name), element('small', '#' + tool.seq + time + code));
      if (tool.command) item.append(element('code', tool.command));
      list.append(item);
    }
    const tools = element('details');
    tools.open = open;
    tools.append(element('summary', '工具明细 · ' + meta.tools.length), list);
    detail.append(tools);
  }

  function create(panel, fetchGraph) {
    const svg = panel.querySelector('svg'), world = svg.querySelector('.dag-world');
    const status = panel.querySelector('.dag-load-status'), detail = panel.querySelector('.dag-detail');
    const graphics = new Map(), pointers = new Map();
    const camera = {x: 0, y: 0, scale: 1, mode: 'initial'};
    let screen = {width: 1, height: 1};
    let taskId, graph, signature, selected, detailSignature, pendingGraph;
    let generation = 0, timer = null, inFlight = false, queued = false, dragged = false;

    const markers = svgElement('defs');
    for (const kind of edgeKinds) {
      const marker = svgElement('marker', {
        id: 'dag-arrow-' + kind, viewBox: '0 0 8 8', markerWidth: '6',
        markerHeight: '6', refX: '8', refY: '4', orient: 'auto', class: 'dag-edge-' + kind,
      });
      marker.append(svgElement('path', {d: 'M0 0 L8 4 L0 8 Z', fill: 'currentColor'}));
      markers.append(marker);
    }
    svg.prepend(markers);

    function transform() {
      world.setAttribute('transform', `translate(${camera.x} ${camera.y}) scale(${camera.scale})`);
    }

    function fit() {
      if (!graph) return;
      const scale = Math.min(screen.width / graph.width, screen.height / graph.height, 1);
      const minimum = screen.width < 500 ? .68 : .55;
      if (camera.mode === 'initial' && scale < minimum) {
        const first = graph.nodes[0];
        camera.scale = minimum;
        camera.x = 8;
        camera.y = screen.height / 2 - ((first?.y ?? 0) + height / 2) * minimum;
      } else {
        camera.scale = Math.max(.01, scale);
        camera.x = (screen.width - graph.width * camera.scale) / 2;
        camera.y = (screen.height - graph.height * camera.scale) / 2;
      }
      transform();
    }

    function resize() {
      const box = svg.getBoundingClientRect();
      if (!box.width || !box.height) return;
      screen = {width: box.width, height: box.height};
      svg.setAttribute('viewBox', `0 0 ${screen.width} ${screen.height}`);
      if (camera.mode !== 'manual') fit();
    }

    function zoom(factor, x = screen.width / 2, y = screen.height / 2, offsetX = 0, offsetY = 0) {
      if (!graph) return;
      const scale = Math.max(.01, Math.min(3, camera.scale * factor));
      const used = scale / camera.scale;
      camera.x = x + (camera.x - x) * used + offsetX;
      camera.y = y + (camera.y - y) * used + offsetY;
      camera.scale = scale;
      camera.mode = 'manual';
      transform();
    }

    function pan(x, y) {
      camera.mode = 'manual';
      camera.x += x;
      camera.y += y;
      transform();
    }

    function drawingNode(item) {
      const {node, x, y} = item;
      const group = svgElement('g', {
        transform: `translate(${x} ${y})`, class: 'dag-node', role: 'button', tabindex: 0,
        'data-node': node.id, 'data-status': node.status,
        'aria-label': node.label + ' · ' + statusLabels[node.status],
        'aria-pressed': String(selected === node.id),
      });
      const parts = [...node.label];
      const label = parts.length > 20 ? parts.slice(0, 19).join('') + '…' : node.label;
      group.append(
        svgElement('title', {}, node.label),
        svgElement('rect', {width, height, rx: 9}),
        svgElement('circle', {cx: 15, cy: 17, r: 4}),
        svgElement('text', {x: 26, y: 22, class: 'dag-node-label'}, label),
        svgElement('text', {x: 14, y: 45, class: 'dag-node-state'},
                   kindLabels[node.kind] + ' · ' + statusLabels[node.status]),
      );
      return group;
    }

    function showDetail() {
      const item = graphics.get(selected);
      const node = item?.node;
      const nextSignature = node ? JSON.stringify(node) : null;
      if (detailSignature === nextSignature) return;
      const toolsOpen = detail.querySelector('details')?.open ?? true;
      detailSignature = nextSignature;
      detail.hidden = !node;
      if (!node) {
        detail.replaceChildren();
        return;
      }
      const heading = element('h3', node.label);
      const facts = element('dl');
      const meta = node.meta ?? {};
      function fact(label, value) {
        facts.append(element('dt', label), element('dd', value ?? '—'));
      }
      fact('状态', statusLabels[node.status]);
      fact('开始', describeTime(node.started_at));
      fact('结束', describeTime(node.finished_at));
      fact('时长', node.duration_s == null ? null : node.duration_s.toFixed(2) + ' 秒');
      fact('费用', node.cost_usd == null ? null : '$' + node.cost_usd.toFixed(6));
      fact('Token', node.tokens?.toLocaleString('zh-CN'));
      if (meta.depends_on?.length) fact('依赖', meta.depends_on.join('、'));
      if (meta.model) fact('模型', meta.model);
      if (meta.merge_status) fact('合并状态', meta.merge_status);
      if (meta.decision) fact('审核决定', {approve: '通过', reject: '拒绝', request_changes: '要求修改'}[meta.decision]);
      detail.replaceChildren(heading, facts);
      const childId = meta.child_task_id || (node.task_id !== taskId ? node.task_id : null);
      if (childId) {
        const anchor = element('a', '打开子任务 ↗');
        anchor.href = '#/tasks/' + encodeURIComponent(childId);
        detail.append(anchor);
      }
      const note = meta.text || meta.feedback || meta.reason || meta.message;
      if (note) detail.append(element('p', note));
      const checks = Object.values(meta.stages ?? {}).flatMap(stage => {
        if (Array.isArray(stage.checks) && stage.checks.length) return stage.checks;
        return stage.command || stage.summary ? [{command: stage.command || stage.summary, passed: stage.passed}] : [];
      });
      if (checks.length) {
        const list = element('ul', undefined, 'dag-checks');
        for (const check of checks) {
          const result = check.passed === true ? '通过 · ' : check.passed === false ? '失败 · ' : '';
          list.append(element('li', result + (check.command || check.name || '检查')));
        }
        detail.append(list);
      }
      appendTools(detail, meta, toolsOpen);
    }

    function select(id) {
      const previous = graphics.get(selected);
      const next = graphics.get(id);
      if (!next) return;
      previous?.element.setAttribute('aria-pressed', 'false');
      selected = id;
      next.element.setAttribute('aria-pressed', 'true');
      showDetail();
    }

    function render(dag) {
      if (pointers.size) {
        pendingGraph = dag;
        return;
      }
      pendingGraph = null;
      const nextSignature = JSON.stringify(dag);
      if (signature === nextSignature) return;
      signature = nextSignature;
      graph = layout(dag);
      const focused = document.activeElement?.getAttribute('data-node');
      graphics.clear();
      const edges = svgElement('g', {'aria-hidden': 'true'});
      for (const edge of graph.edges) {
        edges.append(svgElement('path', {
          d: edgePath(edge), class: 'dag-edge dag-edge-' + edge.kind,
          'marker-end': 'url(#dag-arrow-' + edge.kind + ')',
        }));
      }
      const nodes = svgElement('g');
      for (const item of graph.nodes) {
        item.element = drawingNode(item);
        graphics.set(item.node.id, item);
        nodes.append(item.element);
      }
      world.replaceChildren(edges, nodes);
      graphics.get(focused)?.element.focus({preventScroll: true});
      showDetail();
      resize();
    }

    function reset() {
      generation++;
      clearTimeout(timer);
      timer = null;
      inFlight = queued = false;
      taskId = graph = signature = selected = detailSignature = null;
      pendingGraph = null;
      pointers.clear();
      camera.mode = 'initial';
      status.textContent = '';
      detail.replaceChildren();
      detail.hidden = true;
      world.replaceChildren();
      graphics.clear();
    }

    function refresh(id, fromStream = false) {
      if (id !== taskId) {
        reset();
        taskId = id;
        status.textContent = '正在读取执行图…';
      }
      if (timer !== null || inFlight) {
        if (inFlight && fromStream) queued = true;
        return;
      }
      const current = generation;
      timer = setTimeout(async () => {
        timer = null;
        inFlight = true;
        try {
          const dag = await fetchGraph(id);
          if (current === generation) {
            render(dag);
            status.textContent = '';
          }
        } catch {
          if (current === generation) status.textContent = '暂时无法读取执行图，稍后会重试。';
        } finally {
          if (current === generation) {
            inFlight = false;
            if (queued) {
              queued = false;
              refresh(id, true);
            }
          }
        }
      }, fromStream ? 300 : 0);
    }

    panel.querySelector('.dag-zoom-in').onclick = () => zoom(1.3);
    panel.querySelector('.dag-zoom-out').onclick = () => zoom(1 / 1.3);
    panel.querySelector('.dag-fit').onclick = () => {
      camera.mode = 'fit';
      fit();
    };
    svg.addEventListener('click', event => {
      if (dragged && event.detail) return;
      const node = event.target.closest('[data-node]');
      if (node) select(node.getAttribute('data-node'));
    });
    svg.addEventListener('keydown', event => {
      const id = event.target.getAttribute('data-node');
      if (id && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        select(id);
        return;
      }
      const direction = {ArrowLeft: [64, 0], ArrowRight: [-64, 0],
                         ArrowUp: [0, 64], ArrowDown: [0, -64]}[event.key];
      if (direction) pan(...direction);
      else if (event.key === '+' || event.key === '=') zoom(1.3);
      else if (event.key === '-') zoom(1 / 1.3);
      else return;
      event.preventDefault();
    });
    function point(event) {
      const box = svg.getBoundingClientRect();
      return {x: event.clientX - box.left, y: event.clientY - box.top};
    }
    svg.addEventListener('wheel', event => {
      if (!graph) return;
      event.preventDefault();
      const {x, y} = point(event);
      zoom(event.deltaY > 0 ? 1 / 1.16 : 1.16, x, y);
    }, {passive: false});
    svg.addEventListener('pointerdown', event => {
      if (!graph || event.button !== 0 || pointers.size >= 2) return;
      if (!pointers.size) dragged = false;
      pointers.set(event.pointerId, point(event));
      event.target.setPointerCapture(event.pointerId);
    });
    svg.addEventListener('pointermove', event => {
      const before = pointers.get(event.pointerId);
      if (!before) return;
      const after = point(event);
      const dx = after.x - before.x, dy = after.y - before.y;
      if (pointers.size === 2) {
        const other = [...pointers].find(([id]) => id !== event.pointerId)[1];
        const factor = Math.hypot(after.x - other.x, after.y - other.y) /
          Math.max(1, Math.hypot(before.x - other.x, before.y - other.y));
        dragged = true;
        zoom(factor, (before.x + other.x) / 2, (before.y + other.y) / 2, dx / 2, dy / 2);
      } else if (dragged || Math.hypot(dx, dy) >= 4) {
        dragged = true;
        pan(dx, dy);
      } else return;
      pointers.set(event.pointerId, after);
    });
    for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      svg.addEventListener(name, event => {
        pointers.delete(event.pointerId);
        if (!pointers.size && pendingGraph) {
          requestAnimationFrame(() => {
            if (!pointers.size && pendingGraph) render(pendingGraph);
          });
        }
      });
    }
    const observer = new ResizeObserver(resize);
    observer.observe(svg);
    return {refresh, reset};
  }

  return {create, layout};
})();
if (typeof module !== 'undefined') module.exports = SCATaskDAG;
