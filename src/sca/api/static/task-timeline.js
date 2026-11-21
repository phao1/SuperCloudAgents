/* The single-row timeline follows awesomeTelemetry's 2d0928d task reader. */
const SCATaskTimeline = (() => {
  const input = new Set(['task.created', 'agent.turn_started', 'review.submitted', 'user.message']);
  const model = new Set(['agent.message', 'agent.usage']);
  const tools = new Set(['agent.tool', 'agent.command', 'agent.file_change', 'verification.started',
    'verification.completed', 'acceptance.completed', 'ci.requested', 'ci.completed', 'intents.checked']);
  const failures = new Set(['agent.error', 'plan.rejected', 'budget.exceeded']);
  const labels = { input: '输入', model: '模型', tool: '工具', context: '上下文' };
  const svgWidth = 1000;
  const sequenceHelp = '按事件顺序等宽展示。拖动放大选区，滚轮缩放，右键拖动平移。';
  const durationHelp = '按记录时间排列。耗时未知的事件显示为点。拖动可筛选事件记录。';
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    fractionalSecondDigits: 3, hourCycle: 'h23',
  });

  const nonnegative = value => Number.isFinite(value) && value >= 0;
  const timestamp = value => nonnegative(value) && value <= 8.64e15 ? value : null;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const intersects = ([start, end], [from, to]) => start <= to && end >= from;

  function buildRecords(events) {
    return events.map((event, index) => {
      const { type } = event, p = event.payload || {};
      const kind = input.has(type) ? 'input' : model.has(type) ? 'model' : tools.has(type) ? 'tool' : 'context';
      const start = timestamp(p.started_at_ms), end = timestamp(p.ended_at_ms);
      const reported = type === 'agent.usage' ? p.model_call_duration_ms
        : nonnegative(p.duration_ms) ? p.duration_ms : nonnegative(p.duration_s) ? p.duration_s * 1000 : null;
      const duration = nonnegative(reported) ? reported : null;
      const observed = start ?? end ?? timestamp(Date.parse(event.created_at));
      let interval = observed === null ? null : [observed, observed];
      if (start !== null && end !== null && end >= start) {
        interval = [start, type === 'agent.usage' && duration !== null ? Math.min(end, start + duration) : end];
      } else if (start !== null && duration !== null && timestamp(start + duration) !== null) {
        interval = [start, start + duration];
      } else if (end !== null && duration !== null && timestamp(end - duration) !== null) {
        interval = [end - duration, end];
      }
      return { event, index, kind, interval, duration, loggedEnd: end,
        boundary: type === 'task.created' || type === 'agent.turn_started',
        failed: failures.has(type) || p.passed === false || p.is_error === true || p.status === 'failed'
          || (typeof p.exit_code === 'number' && p.exit_code !== 0)
          || (type === 'task.state_changed' && p.to === 'FAILED') };
    });
  }

  function intervalLength(intervals) {
    let end = -Infinity, total = 0;
    for (const [start, stop] of intervals.sort((a, b) => a[0] - b[0])) {
      total += Math.max(0, stop - Math.max(start, end));
      end = Math.max(end, stop);
    }
    return total;
  }

  function activeTiming(records) {
    let start = Infinity, end = -Infinity;
    const model = [], tools = [];
    for (const record of records) {
      const { event, kind, interval, loggedEnd } = record;
      if (!interval || interval[0] === interval[1]) continue;
      if (event.type !== 'agent.usage' && (kind !== 'tool' || !event.type.startsWith('agent.'))) continue;
      (event.type === 'agent.usage' ? model : tools).push(interval);
      start = Math.min(start, interval[0]);
      end = Math.max(end, interval[1], loggedEnd ?? interval[1]);
    }
    if (!Number.isFinite(start)) return null;
    const extent = end - start, modelMs = intervalLength(model), occupied = intervalLength([...model, ...tools]);
    return { extent, model: modelMs / extent * 100, tools: (occupied - modelMs) / extent * 100,
      other: (extent - occupied) / extent * 100 };
  }

  function create(root, { titleFor, onSelect, onRangeChange }) {
    const find = selector => root.querySelector(selector);
    const track = find('svg'), marks = find('.task-time-marks'), rounds = find('.task-time-rounds');
    const brush = find('.task-time-selection'), tooltip = find('.task-time-tooltip');
    const choices = find('.task-time-choices'), choiceList = find('.task-time-choice-list');
    const mode = find('.task-time-mode'), count = find('.task-time-count'), empty = find('.task-time-empty');
    const clear = find('.task-time-clear'), plus = find('.task-time-zoom-in');
    const minus = find('.task-time-zoom-out'), reset = find('.task-time-reset');
    let taskId, records = [], available = [], positions = new Map(), boxes = new Map(), timing;
    let duration = false, domain = [0, 1], view = null, range = null, selected = null;
    let gesture = null, hover = null, hoverTimer;

    function element(tag, attributes) {
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
      return node;
    }

    function fit([from, to]) {
      const width = clamp(to - from, 1, domain[1] - domain[0]);
      if (width === domain[1] - domain[0]) return null;
      const start = clamp(from, domain[0], domain[1] - width);
      return [start, start + width];
    }

    function position(value) {
      const [from, to] = view ?? domain;
      return clamp((value - from) / (to - from), 0, 1) * svgWidth;
    }

    function boxFor([start, end]) {
      const left = position(start), span = position(end) - left;
      const gap = Math.min(1, span / 4);
      const width = duration ? Math.max(5, span) : span - gap;
      return { x: Math.min(left, svgWidth - width), width };
    }

    function drawBrush(selection = range) {
      brush.setAttribute('width', '0');
      if (!selection || !intersects(selection, view ?? domain)) return;
      const x = Math.min(svgWidth - 1, position(selection[0]));
      brush.setAttribute('x', x);
      brush.setAttribute('width', Math.max(1, position(selection[1]) - x));
    }

    function detail(record) {
      if (!record.interval) return '时间未提供';
      const [start, end] = record.interval;
      const ms = end > start ? end - start : record.duration;
      const span = timeFormat.format(start) + (end > start ? ' → ' + timeFormat.format(end) : '');
      return span + ' · ' + (ms === null ? '耗时未提供' : Number(ms.toFixed(2)) + ' 毫秒');
    }

    function name(record) {
      return '#' + record.event.seq + ' · ' + titleFor(record.event);
    }

    function clearHover() {
      clearTimeout(hoverTimer);
      hover = null;
      tooltip.hidden = true;
    }

    function showHover(box) {
      if (box === hover) return;
      clearHover();
      if (!box || !choices.hidden) return;
      hover = box;
      hoverTimer = setTimeout(() => {
        tooltip.querySelector('strong').textContent = box.record.title;
        tooltip.querySelector('small').textContent = box.record.detail;
        tooltip.style.setProperty('--anchor', box.x / svgWidth * 100 + '%');
        tooltip.hidden = false;
      }, 250);
    }

    function closeChoices() {
      if (choices.hidden) return;
      if (choices.contains(document.activeElement)) track.focus({ preventScroll: true });
      choices.hidden = true;
    }

    function paintSelection() {
      for (const [seq, { node }] of boxes) node.setAttribute('aria-pressed', seq === selected);
      const active = boxes.get(selected);
      if (active && active.node !== marks.lastElementChild) marks.append(active.node);
    }

    function draw() {
      const focused = Number(document.activeElement?.dataset?.seq);
      const visible = view ?? domain, blocks = [], separators = [];
      clearHover();
      closeChoices();
      boxes = new Map();
      for (const record of available) {
        const bounds = positions.get(record.event.seq);
        if (!intersects(bounds, visible) || (!duration && (bounds[1] === visible[0] || bounds[0] === visible[1]))) continue;
        const { x, width } = boxFor(bounds), seq = record.event.seq;
        if (record.boundary && bounds[0] >= visible[0]) {
          separators.push(element('line', { x1: x, x2: x, y1: 0, y2: 28 }));
        }
        const node = element('rect', { 'data-seq': seq, 'data-kind': record.kind,
          'data-error': record.failed, 'data-point': duration && bounds[0] === bounds[1],
          x, width, y: 7, height: 14, rx: 1, role: 'button', tabindex: -1,
          'aria-label': record.title + ' · ' + record.detail });
        blocks.push(node);
        boxes.set(seq, { node, record, x, width });
      }
      rounds.replaceChildren(...separators);
      marks.replaceChildren(...blocks);
      paintSelection();
      drawBrush();
      if (Number.isFinite(focused)) (boxes.get(focused)?.node ?? track).focus({ preventScroll: true });
      const unknown = duration ? records.length - available.length : 0;
      count.textContent = records.length.toLocaleString('zh-CN') + ' 条' + (unknown ? ' · ' + unknown + ' 条无时间' : '');
      count.title = unknown ? unknown + ' 条事件时间未知，可切回顺序查看' : '已读取事件';
      empty.textContent = records.length ? '暂无可绘制的时间记录，切回顺序查看。' : '暂无执行事件。';
      empty.hidden = Boolean(available.length);
      mode.setAttribute('aria-pressed', duration);
      mode.title = duration ? durationHelp : sequenceHelp;
      mode.disabled = !records.length;
      plus.disabled = !available.length || visible[1] - visible[0] <= (duration ? 20 : 4);
      minus.disabled = !view;
      reset.disabled = !view && !range;
    }

    function updateDomain() {
      positions = new Map();
      let first = Infinity, last = -Infinity;
      available = records.filter(record => {
        const bounds = duration ? record.interval : [record.index, record.index + 1];
        if (!bounds) return false;
        positions.set(record.event.seq, bounds);
        first = Math.min(first, bounds[0]);
        last = Math.max(last, bounds[1]);
        return true;
      });
      domain = available.length ? [first, Math.max(first + 1, last)] : [0, 1];
      if (view) view = fit(view);
      draw();
    }

    function includes(seq) {
      if (!range) return true;
      const bounds = positions.get(seq);
      return Boolean(bounds && intersects(bounds, range));
    }

    function setRange(next) {
      if (!next && !range) return;
      range = next;
      clear.hidden = !range;
      clear.title = range ? timeFormat.format(range[0]) + ' → ' + timeFormat.format(range[1]) : '';
      reset.disabled = !view && !range;
      drawBrush();
      onRangeChange();
    }

    function setView(next) {
      const fitted = next ? fit(next) : null;
      if (view?.[0] === fitted?.[0] && view?.[1] === fitted?.[1]) return;
      view = fitted;
      draw();
    }

    function zoom(factor, anchor = 0.5) {
      if (!available.length) return;
      const [from, to] = view ?? domain, point = from + (to - from) * anchor;
      const minimum = Math.min(duration ? 20 : 4, to - from);
      const width = Math.max(minimum, (to - from) * factor);
      setView([point - width * anchor, point + width * (1 - anchor)]);
    }

    function reveal(seq) {
      const bounds = positions.get(seq);
      if (!bounds || intersects(bounds, view ?? domain) && boxes.has(seq)) return;
      const [from, to] = view ?? domain, half = (to - from) / 2;
      setView([bounds[0] - half, bounds[0] + half]);
    }

    function select(seq) {
      selected = seq;
      if (!includes(seq)) setRange(null);
      reveal(seq);
      paintSelection();
    }

    function activate(seq) {
      closeChoices();
      clearHover();
      track.focus({ preventScroll: true });
      onSelect(seq);
    }

    function pick(seq, fraction) {
      const x = fraction * svgWidth;
      const hits = duration ? [...boxes.values()].filter(box => x >= box.x && x <= box.x + box.width) : [];
      if (hits.length < 2) { activate(seq); return; }
      choices.style.setProperty('--anchor', fraction * 100 + '%');
      choices.querySelector('strong').textContent = '重叠事件 · ' + hits.length;
      choiceList.replaceChildren(...hits.map(({ record }) => {
        const option = document.createElement('button'), choiceSeq = record.event.seq;
        option.type = 'button';
        option.textContent = name(record) + (record.failed ? ' · 失败' : '');
        option.title = option.textContent;
        option.dataset.kind = record.kind;
        option.setAttribute('aria-pressed', choiceSeq === selected);
        option.onclick = () => activate(choiceSeq);
        return option;
      }));
      choices.hidden = false;
      choiceList.firstElementChild.focus({ preventScroll: true });
    }

    function resetView() {
      setRange(null);
      setView(null);
    }

    function fractionAt(event) {
      const rect = track.getBoundingClientRect();
      return clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    }

    const targetSeq = event => Number(event.target.dataset.seq);
    const timeAt = (fraction, [from, to]) => from + fraction * (to - from);
    const dragBounds = (a, b) => [Math.min(a, b), Math.max(a, b)];

    track.addEventListener('pointerdown', event => {
      if (!boxes.size || (event.button !== 0 && event.button !== 2) || !event.isPrimary) return;
      event.preventDefault();
      clearHover();
      closeChoices();
      track.focus({ preventScroll: true });
      const fraction = fractionAt(event), bounds = view ?? domain;
      gesture = { id: event.pointerId, x: event.clientX, fraction, bounds, seq: targetSeq(event),
        pan: event.button === 2 || event.altKey };
      track.setPointerCapture(event.pointerId);
    });

    track.addEventListener('pointermove', event => {
      if (!gesture) { showHover(boxes.get(targetSeq(event))); return; }
      if (gesture.id !== event.pointerId || Math.abs(event.clientX - gesture.x) < 4) return;
      const { fraction, bounds, pan } = gesture, next = fractionAt(event);
      if (pan) {
        const offset = (fraction - next) * (bounds[1] - bounds[0]);
        setView([bounds[0] + offset, bounds[1] + offset]);
      } else drawBrush(dragBounds(timeAt(fraction, bounds), timeAt(next, bounds)));
    });

    track.addEventListener('pointerup', event => {
      if (gesture?.id !== event.pointerId) return;
      const { pan, x, seq, fraction, bounds } = gesture, next = fractionAt(event);
      gesture = null;
      drawBrush();
      if (pan) return;
      if (Math.abs(event.clientX - x) < 4) {
        if (boxes.has(seq)) pick(seq, next);
        return;
      }
      const [from, to] = dragBounds(timeAt(fraction, bounds), timeAt(next, bounds));
      if (duration) setRange([Math.floor(from), Math.ceil(to)]);
      else setView([from, to]);
    });

    track.addEventListener('lostpointercapture', () => { gesture = null; drawBrush(); });
    track.addEventListener('pointerleave', clearHover);
    track.addEventListener('focusin', event => showHover(boxes.get(targetSeq(event))));
    track.addEventListener('focusout', clearHover);
    track.addEventListener('contextmenu', event => event.preventDefault());
    track.addEventListener('dblclick', resetView);
    track.addEventListener('click', event => {
      const seq = targetSeq(event);
      if (event.detail === 0 && boxes.has(seq)) activate(seq);
    });
    track.addEventListener('wheel', event => {
      if (!available.length || gesture) return;
      event.preventDefault();
      zoom(Math.exp(clamp(event.deltaY * 0.003, -1, 1)), fractionAt(event));
    }, { passive: false });

    track.addEventListener('keydown', event => {
      const seq = targetSeq(event);
      switch (event.key) {
        case 'ArrowLeft':
        case 'ArrowRight': {
          const current = available.findIndex(record => record.event.seq === (boxes.has(seq) ? seq : selected));
          const direction = event.key === 'ArrowLeft' ? -1 : 1;
          const next = current < 0 ? boxes.keys().next().value
            : available[clamp(current + direction, 0, available.length - 1)]?.event.seq;
          reveal(next);
          boxes.get(next)?.node.focus({ preventScroll: true });
          break;
        }
        case 'ArrowUp':
        case 'ArrowDown': {
          const [from, to] = view ?? domain, offset = (to - from) * (event.key === 'ArrowUp' ? -0.25 : 0.25);
          setView([from + offset, to + offset]);
          break;
        }
        case 'Enter':
        case ' ': {
          const next = boxes.has(seq) ? seq : boxes.has(selected) ? selected : boxes.keys().next().value;
          if (next !== undefined) activate(next);
          break;
        }
        case '+': case '=': zoom(0.5); break;
        case '-': zoom(2); break;
        case 'Home': case '0': resetView(); break;
        case 'Escape':
          if (!range && !gesture && tooltip.hidden) return;
          gesture = null;
          setRange(null);
          drawBrush();
          clearHover();
          event.stopPropagation();
          break;
        default: return;
      }
      event.preventDefault();
    });

    mode.onclick = () => { duration = !duration; view = null; setRange(null); updateDomain(); };
    plus.onclick = () => zoom(0.5);
    minus.onclick = () => zoom(2);
    reset.onclick = resetView;
    clear.onclick = () => { setRange(null); track.focus({ preventScroll: true }); };
    find('.task-time-close').onclick = closeChoices;
    choices.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.stopPropagation(); closeChoices(); }
    });

    function update(id, events) {
      if (taskId === id && records.length === events.length) return timing;
      if (taskId !== id) {
        taskId = id;
        duration = false;
        view = selected = range = gesture = null;
        clear.hidden = true;
      }
      records = buildRecords(events);
      for (const record of records) {
        record.title = labels[record.kind] + ' · ' + name(record) + (record.failed ? ' · 失败' : '');
        record.detail = detail(record);
      }
      timing = activeTiming(records);
      updateDomain();
      return timing;
    }

    return { update, select, includes, selectedSeq: () => selected };
  }

  return { create, buildRecords, activeTiming };
})();

if (typeof module === 'object') module.exports = SCATaskTimeline;
