'use strict';

const SCATaskDAG = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const nodeWidth = 216, nodeHeight = 60, summaryHeight = 84, margin = 24;
  const minScale = .85;
  const statusLabels = {
    pending: '待执行', running: '运行中', succeeded: '已完成',
    failed: '失败', cancelled: '已取消', skipped: '已跳过', queued: '排队中', stopped: '已停止',
  };
  const kindLabels = {
    task: '任务', plan: '规划', subtask: '子任务', attempt: '候选', execution: '执行', turn: 'Agent 轮次',
    tool_group: '工具', verification: '验证', fix: '修复轮次', interjection: '插话',
    merge: '汇合', review: '人工审核', pr: 'PR', ci: 'CI', finish: '结束',
    evolution: '演进', generation: '代', candidate: '候选', eval_run: '评测',
    case_task: '用例任务', promotion: '晋升决定', champion: '冠军',
  };
  const edgeKinds = ['sequence', 'dependency', 'spawn', 'select', 'merge'];
  const edgePriority = {sequence: 0, spawn: 1, merge: 2, dependency: 3, select: 4};
  const executionKinds = new Set(['turn', 'fix', 'tool_group', 'verification']);
  const edgeKey = (from, to) => JSON.stringify([from, to]);
  const percentFormat = new Intl.NumberFormat('zh-CN', {style: 'percent', maximumFractionDigits: 1});
  let nextRenderer = 0;

  function addToList(map, key, item) {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(item);
  }

  function counts(steps) {
    const summary = {turns: 0, tools: 0, verification: 0};
    for (const step of steps) {
      if (step.kind === 'turn' || step.kind === 'fix') summary.turns++;
      if (step.kind === 'tool_group') summary.tools += step.meta?.tools?.length ?? 0;
      if (step.kind === 'verification') summary.verification++;
    }
    return summary;
  }

  function totalMetric(steps, field) {
    let total = null;
    for (const step of steps) {
      if (Number.isFinite(step[field])) total = (total ?? 0) + step[field];
    }
    return total;
  }

  function executionNode(id, scope, steps) {
    const summary = counts(steps);
    const outcomes = steps.filter(step => step.kind !== 'tool_group' && step.status !== 'skipped');
    const status = outcomes.some(step => step.status === 'running') ? 'running'
      : (outcomes.at(-1)?.status || 'skipped');
    const started_at = steps.map(step => step.started_at).filter(Boolean).sort().at(0);
    const finished_at = status === 'running' || status === 'pending' ? null
      : steps.map(step => step.finished_at).filter(Boolean).sort().at(-1);
    const duration = started_at && finished_at ? Date.parse(finished_at) - Date.parse(started_at) : null;
    const title = scope.kind === 'task' ? '主任务' : scope.label + ' · ';
    const activity = summary.turns ? `执行 · ${summary.turns} 轮` : '验证';
    return {
      id, kind: 'execution', label: title + activity, status, summary,
      task_id: scope.task_id, started_at, finished_at,
      duration_s: duration === null ? null : Math.max(0, duration / 1000),
      cost_usd: totalMetric(steps, 'cost_usd'), tokens: totalMetric(steps, 'tokens'),
    };
  }

  function remapEdges(edges, source, target = source) {
    const mapped = new Map();
    for (const edge of edges) {
      const from = source(edge.from), to = target(edge.to);
      if (from === to) continue;
      const key = edgeKey(from, to), existing = mapped.get(key);
      if (!existing || edgePriority[edge.kind] > edgePriority[existing.kind]) {
        mapped.set(key, {from, to, kind: edge.kind});
      }
    }
    return mapped;
  }

  function projectGraph(dag, expanded = new Set()) {
    const byId = new Map(dag.nodes.map(node => [node.id, node]));
    const predecessors = new Map(), attempts = new Map();
    for (const edge of dag.edges) {
      if (!predecessors.has(edge.to)) predecessors.set(edge.to, new Set());
      predecessors.get(edge.to).add(edge.from);
      if (edge.kind === 'spawn' && byId.get(edge.to)?.kind === 'attempt') {
        addToList(attempts, edge.from, edge.to);
      }
    }
    const heads = new Map(), groups = new Map(), scopedSteps = new Map();
    for (const node of dag.nodes) {
      const parent = node.meta?.parent;
      if (!byId.has(parent)) continue;
      addToList(scopedSteps, parent, node);
      // A group has a single entry. A visible checkpoint or a second incoming path starts a new group.
      const path = [];
      let head = node.id;
      while (!heads.has(head)) {
        path.push(head);
        const sources = predecessors.get(head);
        if (sources?.size !== 1) break;
        const [previous] = sources;
        if (previous === parent) {
          if (byId.get(parent).kind !== 'task') head = parent;
          break;
        }
        if (byId.get(previous)?.meta?.parent !== parent) break;
        head = previous;
      }
      head = heads.get(head) ?? head;
      for (const id of path) heads.set(id, head);
      addToList(groups, head, node);
    }

    const hidden = new Map(), headers = new Map(), entries = new Map();
    for (const [head, steps] of groups) {
      const scope = byId.get(steps[0].meta.parent);
      const inScope = head === scope.id;
      if (!inScope && !steps.some(step => executionKinds.has(step.kind))) continue;
      const id = inScope ? head : `${head}/execution`;
      const open = expanded.has(id);
      const header = inScope ? scope : executionNode(id, scope, steps);
      if (!inScope) entries.set(head, id);
      if (!open) for (const step of steps) hidden.set(step.id, id);
      headers.set(id, {...header, collapsible: true, expanded: open});
    }
    for (const node of dag.nodes) {
      if (!headers.has(node.id) && node.kind !== 'subtask' && node.kind !== 'attempt') continue;
      const own = scopedSteps.get(node.id) || [];
      const candidates = attempts.get(node.id) || [];
      const summary = counts([...own, ...candidates.flatMap(id => scopedSteps.get(id) || [])]);
      summary.turns = Math.max(summary.turns, node.meta?.turns || 0);
      const cost = node.cost_usd ?? totalMetric([...own, ...candidates.map(id => byId.get(id))], 'cost_usd');
      headers.set(node.id, {...node, ...headers.get(node.id), cost_usd: cost, summary});
    }
    const nodes = dag.nodes.flatMap(node => {
      const entry = entries.get(node.id);
      return [entry && headers.get(entry), !hidden.has(node.id) && (headers.get(node.id) || node)].filter(Boolean);
    });
    const edges = remapEdges(dag.edges, id => hidden.get(id) ?? id,
      id => hidden.get(id) ?? entries.get(id) ?? id);
    for (const [first, id] of entries) {
      if (expanded.has(id)) edges.set(edgeKey(id, first), {from: id, to: first, kind: 'sequence'});
    }
    const dependencies = new Map();
    for (const edge of edges.values()) {
      if (edge.kind === 'dependency') addToList(dependencies, edge.to, edge.from);
    }
    // A planned subtask with a dependency already has a path from the plan through that dependency.
    return {nodes, edges: [...edges.values()].filter(({from, to, kind}) =>
      kind !== 'spawn' || byId.get(to)?.kind !== 'subtask' ||
      !dependencies.get(to)?.some(need => edges.get(edgeKey(from, need))?.kind === 'spawn'))};
  }

  function projectEvolution(dag, expanded = new Set()) {
    const counts = new Map(), hidden = new Map();
    for (const node of dag.nodes) {
      const parent = node.meta?.parent;
      if (node.kind !== 'case_task' || !parent) continue;
      counts.set(parent, (counts.get(parent) ?? 0) + 1);
      if (!expanded.has(parent)) hidden.set(node.id, parent);
    }
    const nodes = dag.nodes.filter(node => !hidden.has(node.id)).map(node => counts.has(node.id)
      ? {...node, caseCount: counts.get(node.id), collapsible: true, expanded: expanded.has(node.id)} : node);
    return {nodes, edges: [...remapEdges(dag.edges, id => hidden.get(id) ?? id).values()]};
  }

  function scoreLabel(score) {
    return Number.isFinite(score) ? percentFormat.format(score) : null;
  }

  function metricLine(node) {
    if (!Object.hasOwn(node, 'score')) return '';
    const score = scoreLabel(node.score);
    return [score && `通过率 ${score}`, Number.isFinite(node.cost_usd) && '$' + node.cost_usd.toFixed(6)]
      .filter(Boolean).join(' · ');
  }

  function orderLayers(layers) {
    const setOrder = layer => layer.forEach((item, index) => {
      item.order = index - (layer.length - 1) / 2;
    });
    function sweep(ordered, neighbour) {
      for (const layer of ordered) {
        for (const item of layer) {
          const linked = item[neighbour];
          item.centre = linked.length ? linked.reduce((sum, next) => sum + next.order, 0) / linked.length
            : item.order;
        }
        layer.sort((a, b) => a.centre - b.centre);
        setOrder(layer);
      }
    }
    layers.forEach(setOrder);
    for (let pass = 0; pass < 3; pass++) {
      sweep(layers, 'before');
      sweep([...layers].reverse(), 'after');
    }
  }

  function layout(dag, orientation = 'vertical') {
    const horizontal = orientation === 'horizontal';
    const layerGap = horizontal ? 52 : 32, siblingGap = horizontal ? 24 : 34, wireGap = 17;
    const nodes = new Map(dag.nodes.map(node => [node.id, {
      node, incoming: [], outgoing: [], before: [], after: [], remaining: 0, rank: 0,
      width: nodeWidth, height: node.summary || metricLine(node) ? summaryHeight : nodeHeight,
    }]));
    const links = [];
    for (const {from, to, kind} of dag.edges) {
      const source = nodes.get(from), target = nodes.get(to);
      if (!source || !target) continue;
      const edge = {source, target, kind};
      source.outgoing.push(edge);
      target.incoming.push(edge);
      target.remaining++;
      links.push(edge);
    }
    const ready = [...nodes.values()].filter(node => !node.remaining), layers = [];
    for (let index = 0; index < ready.length; index++) {
      const current = ready[index];
      (layers[current.rank] ??= []).push(current);
      for (const {target} of current.outgoing) {
        target.rank = Math.max(target.rank, current.rank + 1);
        if (--target.remaining === 0) ready.push(target);
      }
    }
    const edges = links.filter(({source, target}) => !source.remaining && !target.remaining);
    for (const edge of edges) {
      const {source, target} = edge;
      edge.route = [source];
      // Waypoints reserve space in intermediate layers for edges that skip a layer.
      for (let rank = source.rank + 1; rank < target.rank; rank++) {
        const waypoint = {before: [], after: []};
        layers[rank].push(waypoint);
        edge.route.push(waypoint);
      }
      edge.route.push(target);
      for (let index = 1; index < edge.route.length; index++) {
        const from = edge.route[index - 1], to = edge.route[index];
        from.after.push(to);
        to.before.push(from);
      }
    }
    orderLayers(layers);

    const across = horizontal ? 'height' : 'width', along = horizontal ? 'width' : 'height';
    const widths = layers.map(layer => layer.reduce((span, item, index) => {
      const gap = index === 0 ? 0 : (item.node && layer[index - 1].node ? siblingGap : wireGap);
      item.cross = span + gap + (item[across] || 0) / 2;
      return item.cross + (item[across] || 0) / 2;
    }, 0));
    const span = Math.max(0, ...widths);
    let depth = margin;
    layers.forEach((layer, rank) => {
      const size = Math.max(...layer.map(item => item[along] || 0));
      for (const item of layer) {
        item.cross += margin + (span - widths[rank]) / 2;
        item.layerStart = depth;
        item.layerEnd = depth + size;
        if (!item.node) continue;
        item.entry = depth + (size - item[along]) / 2;
        item.exit = item.entry + item[along];
        const left = item.cross - item[across] / 2;
        item.x = horizontal ? item.entry : left;
        item.y = horizontal ? left : item.entry;
      }
      depth += size + layerGap;
    });
    for (const item of ready) {
      for (const [links, routeIndex, attr] of [
        [item.incoming, -2, 'targetPort'], [item.outgoing, 1, 'sourcePort'],
      ]) {
        const connected = links.filter(edge => edge.route);
        const step = Math.min(16, item[across] / (connected.length + 1));
        connected.sort((a, b) => a.route.at(routeIndex).cross - b.route.at(routeIndex).cross);
        connected.forEach((edge, index) => {
          edge[attr] = item.cross + step * (index - (connected.length - 1) / 2);
        });
      }
    }
    const length = Math.max(margin * 2, depth - layerGap + margin), breadth = span + margin * 2;
    return {nodes: layers.flat().filter(item => item.node), edges, orientation,
      width: horizontal ? length : breadth, height: horizontal ? breadth : length};
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

  function edgePath({source, target, route, sourcePort, targetPort}, orientation) {
    const point = (cross, depth) => orientation === 'horizontal' ? `${depth} ${cross}` : `${cross} ${depth}`;
    let path = `M ${point(sourcePort, source.exit)}`;
    for (let i = 1; i < route.length; i++) {
      const from = route[i - 1], to = route[i];
      const out = i === 1 ? sourcePort : from.cross;
      const into = to === target ? targetPort : to.cross;
      const middle = (from.layerEnd + to.layerStart) / 2;
      path += ` L ${point(out, from.layerEnd)} C ${point(out, middle)}, ${point(into, middle)}, ${point(into, to.layerStart)}`;
    }
    return path + ` L ${point(targetPort, target.entry)}`;
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

  function create(panel, fetchGraph, {project = projectGraph} = {}) {
    const svg = panel.querySelector('svg'), world = svg.querySelector('.dag-world');
    const status = panel.querySelector('.dag-load-status'), detail = panel.querySelector('.dag-detail');
    const panHint = panel.querySelector('.dag-pan-hint');
    const orientationButtons = [...panel.querySelectorAll('[data-dag-orientation]')];
    const graphics = new Map(), pointers = new Map();
    const expanded = new Set();
    const camera = {x: 0, y: 0, scale: 1};
    let screen = {width: 1, height: 1};
    let resourceId, snapshot, graph, signature, selected, detailSignature, pendingGraph;
    let orientation = 'vertical', needsFit = true;
    let generation = 0, timer = null, inFlight = false, queued = false, dragged = false;

    const markerPrefix = `dag-arrow-${++nextRenderer}-`;
    const markers = svgElement('defs');
    for (const kind of edgeKinds) {
      const marker = svgElement('marker', {
        id: markerPrefix + kind, viewBox: '0 0 8 8', markerWidth: '6',
        markerHeight: '6', refX: '8', refY: '4', orient: 'auto', class: 'dag-edge-' + kind,
      });
      marker.append(svgElement('path', {d: 'M0 0 L8 4 L0 8 Z', fill: 'currentColor'}));
      markers.append(marker);
    }
    svg.prepend(markers);

    function constrain(offset, available) {
      return Math.max(Math.min(0, available), Math.min(offset, Math.max(0, available)));
    }

    function transform() {
      if (!graph) return;
      const extraX = screen.width - graph.width * camera.scale, extraY = screen.height - graph.height * camera.scale;
      camera.x = constrain(camera.x, extraX);
      camera.y = constrain(camera.y, extraY);
      panHint.hidden = extraX >= -1 && extraY >= -1;
      world.setAttribute('transform', `translate(${camera.x} ${camera.y}) scale(${camera.scale})`);
    }

    function resize() {
      const width = svg.clientWidth, height = svg.clientHeight;
      if (!graph || !width || !height) return;
      screen = {width, height};
      svg.setAttribute('viewBox', `0 0 ${screen.width} ${screen.height}`);
      if (needsFit) {
        needsFit = false;
        const scale = Math.max(minScale, Math.min(width / graph.width, height / graph.height, 1));
        const extraX = width - graph.width * scale, extraY = height - graph.height * scale;
        const first = graph.nodes[0];
        camera.scale = scale;
        camera.x = extraX < 0 && orientation === 'horizontal' ? 0 : extraX / 2;
        camera.y = extraY < 0 && orientation === 'vertical' ? 0 : extraY / 2;
        if (first && extraX < 0 && orientation === 'vertical') {
          camera.x = width / 2 - (first.x + first.width / 2) * scale;
        }
        if (first && extraY < 0 && orientation === 'horizontal') {
          camera.y = height / 2 - (first.y + first.height / 2) * scale;
        }
      }
      transform();
    }

    function zoom(factor, x = screen.width / 2, y = screen.height / 2, offsetX = 0, offsetY = 0) {
      if (!graph) return;
      const scale = Math.max(minScale, Math.min(3, camera.scale * factor));
      const used = scale / camera.scale;
      camera.x = x + (camera.x - x) * used + offsetX;
      camera.y = y + (camera.y - y) * used + offsetY;
      camera.scale = scale;
      transform();
    }

    function pan(x, y) {
      const previousX = camera.x, previousY = camera.y;
      camera.x += x;
      camera.y += y;
      transform();
      return camera.x !== previousX || camera.y !== previousY;
    }

    function drawingNode({node, x, y, width, height}) {
      const state = statusLabels[node.status];
      const summary = node.summary;
      const activity = summary ? `${summary.turns} 轮 · ${summary.tools} 工具 · ${summary.verification} 验证`
        : metricLine(node);
      const facts = summary ? [state, node.duration_s == null ? '—' : node.duration_s.toFixed(1) + ' 秒',
        node.cost_usd == null ? '—' : '$' + node.cost_usd.toFixed(6)].join(' · ')
        : [kindLabels[node.kind], state, node.caseCount && `${node.caseCount} 个用例`].filter(Boolean).join(' · ');
      const group = svgElement('g', {
        transform: `translate(${x} ${y})`, class: 'dag-node', role: 'button', tabindex: 0,
        'data-node': node.id, 'data-kind': node.kind, 'data-status': node.status,
        'aria-label': [node.label, activity, facts].filter(Boolean).join(' · '),
        'aria-pressed': String(selected === node.id),
      });
      const label = svgElement('foreignObject', {x: 27, y: 8, width: width - (node.collapsible ? 54 : 38),
        height: 25, 'aria-hidden': 'true'});
      label.append(element('div', node.label, 'dag-node-label'));
      const body = svgElement('foreignObject', {x: 13, y: 33, width: width - 26,
        height: height - 33, 'aria-hidden': 'true'});
      if (activity) body.append(element('div', activity, 'dag-node-counts'));
      body.append(element('div', facts, 'dag-node-state'));
      group.append(
        svgElement('title', {}, node.label),
        svgElement('rect', {width, height, rx: 9}),
        svgElement('circle', {cx: 15, cy: 20, r: 4}), label, body,
      );
      if (node.collapsible) {
        group.setAttribute('aria-expanded', String(node.expanded));
        group.append(svgElement('path', {
          class: 'dag-node-chevron', transform: `translate(${width - 18} 20)`,
          d: node.expanded ? 'M -4 -2 L 0 2 L 4 -2' : 'M -2 -4 L 2 0 L -2 4',
          'aria-hidden': 'true',
        }));
      }
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
      if (Number.isFinite(node.score)) fact('通过率', scoreLabel(node.score));
      if (node.caseCount) fact('用例数', node.caseCount);
      if (node.summary) {
        const {turns, tools, verification} = node.summary;
        fact('轮次', turns);
        fact('工具调用', tools);
        fact('验证', verification);
      }
      if (meta.depends_on?.length) fact('依赖', meta.depends_on.join('、'));
      if (meta.model) fact('模型', meta.model);
      if (meta.merge_status) fact('合并状态', meta.merge_status);
      if (meta.split) fact('评测分组', {dev: '开发集', holdout: '留出集'}[meta.split]);
      if (meta.decision) {
        if (node.kind === 'candidate' || node.kind === 'promotion') {
          fact('晋升决定', {promoted: '已晋升', rejected: '未晋升', invalid: '静态检查未通过', pending: '待定'}[meta.decision]);
        } else fact('审核决定', {approve: '通过', reject: '拒绝', request_changes: '要求修改'}[meta.decision]);
      }
      detail.replaceChildren(heading, facts);
      const childId = meta.child_task_id || (node.task_id !== resourceId ? node.task_id : null);
      if (childId) {
        const anchor = element('a', node.kind === 'case_task' ? '打开用例任务 ↗' : '打开子任务 ↗');
        anchor.href = '#/tasks/' + encodeURIComponent(childId);
        detail.append(anchor);
      }
      if (meta.run_id || meta.champion_id) {
        const anchor = element('a', meta.run_id ? '打开评测 ↗' : '打开冠军记录 ↗');
        anchor.href = meta.run_id ? '#/runs/' + encodeURIComponent(meta.run_id)
          : '#/evolutions/champions/' + encodeURIComponent(meta.champion_id);
        detail.append(anchor);
      }
      if (meta.hypothesis) detail.append(element('p', meta.hypothesis));
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

    function activate(id) {
      const previous = graphics.get(selected);
      const next = graphics.get(id);
      if (!next) return;
      previous?.element.setAttribute('aria-pressed', 'false');
      selected = id;
      if (next.node.collapsible) {
        if (!expanded.delete(id)) expanded.add(id);
        redraw(id);
        return;
      }
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
      snapshot = dag;
      redraw();
    }

    function anchorNearCenter() {
      const x = (screen.width / 2 - camera.x) / camera.scale;
      const y = (screen.height / 2 - camera.y) / camera.scale;
      let closest, distance = Infinity;
      for (const item of graphics.values()) {
        const offset = Math.hypot(item.x + item.width / 2 - x, item.y + item.height / 2 - y);
        if (offset < distance) {
          closest = item;
          distance = offset;
        }
      }
      return closest;
    }

    function redraw(anchorId) {
      const focused = document.activeElement?.getAttribute('data-node');
      const anchor = graphics.get(anchorId) || anchorNearCenter();
      graph = layout(project(snapshot, expanded), orientation);
      graphics.clear();
      const edges = svgElement('g', {'aria-hidden': 'true'});
      for (const edge of graph.edges) {
        edges.append(svgElement('path', {
          d: edgePath(edge, orientation), class: 'dag-edge dag-edge-' + edge.kind,
          'marker-end': 'url(#' + markerPrefix + edge.kind + ')',
        }));
      }
      const nodes = svgElement('g');
      for (const item of graph.nodes) {
        item.element = drawingNode(item);
        graphics.set(item.node.id, item);
        nodes.append(item.element);
      }
      world.replaceChildren(edges, nodes);
      const nextAnchor = graphics.get(anchor?.node.id);
      if (nextAnchor && !needsFit) {
        camera.x += (anchor.x - nextAnchor.x) * camera.scale;
        camera.y += (anchor.y - nextAnchor.y) * camera.scale;
      }
      svg.style.setProperty('--dag-height', `${graph.height}px`);
      resize();
      graphics.get(focused)?.element.focus({preventScroll: true});
      showDetail();
    }

    function reset() {
      generation++;
      clearTimeout(timer);
      timer = null;
      inFlight = queued = false;
      resourceId = snapshot = graph = signature = selected = detailSignature = null;
      pendingGraph = null;
      pointers.clear();
      expanded.clear();
      needsFit = true;
      status.textContent = '';
      detail.replaceChildren();
      detail.hidden = true;
      panHint.hidden = true;
      world.replaceChildren();
      svg.style.removeProperty('--dag-height');
      graphics.clear();
    }

    function refresh(id, coalesce = false) {
      if (id !== resourceId) {
        reset();
        resourceId = id;
        status.textContent = '正在读取执行图…';
      }
      if (timer !== null || inFlight) {
        if (inFlight && coalesce) queued = true;
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
      }, coalesce ? 300 : 0);
    }

    panel.querySelector('.dag-zoom-in').onclick = () => zoom(1.3);
    panel.querySelector('.dag-zoom-out').onclick = () => zoom(1 / 1.3);
    panel.querySelector('.dag-fit').onclick = () => {
      needsFit = true;
      resize();
    };
    for (const button of orientationButtons) {
      button.onclick = () => {
        orientation = button.dataset.dagOrientation;
        for (const option of orientationButtons) {
          option.setAttribute('aria-pressed', String(option === button));
        }
        needsFit = true;
        if (snapshot) redraw();
      };
    }
    svg.addEventListener('click', event => {
      if (dragged && event.detail) return;
      const node = event.target.closest('[data-node]');
      if (node) activate(node.getAttribute('data-node'));
    });
    svg.addEventListener('keydown', event => {
      const id = event.target.getAttribute('data-node');
      if (id && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        activate(id);
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
    svg.addEventListener('focusin', event => {
      if (!event.target.matches(':focus-visible')) return;
      const item = graphics.get(event.target.getAttribute('data-node'));
      if (!item) return;
      const left = camera.x + item.x * camera.scale, top = camera.y + item.y * camera.scale;
      const right = left + item.width * camera.scale, bottom = top + item.height * camera.scale;
      pan(Math.max(0, 8 - left) - Math.max(0, right + 8 - screen.width),
          Math.max(0, 8 - top) - Math.max(0, bottom + 8 - screen.height));
    });
    function point(event) {
      const box = svg.getBoundingClientRect();
      return {x: event.clientX - box.left, y: event.clientY - box.top};
    }
    svg.addEventListener('wheel', event => {
      if (!graph) return;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        const {x, y} = point(event);
        zoom(event.deltaY > 0 ? 1 / 1.16 : 1.16, x, y);
        return;
      }
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? screen.height : 1;
      const sideways = orientation === 'horizontal' && !event.deltaX && graph.height * camera.scale <= screen.height;
      const dx = sideways ? event.deltaY : event.deltaX, dy = sideways ? 0 : event.deltaY;
      if (pan(-dx * unit, -dy * unit)) event.preventDefault();
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
    return {refresh, reset, destroy() {reset(); observer.disconnect();}};
  }

  return {create, layout, projectGraph, projectEvolution};
})();
if (typeof module !== 'undefined') module.exports = SCATaskDAG;
