(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const { meta, modules } = JSON.parse($("milestone-data").textContent);
  const repositoryDirectories = new Set(JSON.parse($("repository-directories").textContent));
  const states = {
    done: ["已有实现", "主要职责在此快照中有相应实现；状态不代表生产验证。"],
    partial: ["部分实现", "已有机制、接口或部分场景，仍有明确的覆盖或验证缺口。"],
    adapted: ["架构替代", "V2 用不同的架构覆盖相关职责；详情指出替代方案与 V1 差异。"],
    planned: ["尚未实现", "未找到对应的独立能力；详情可包含邻近实现与后续方向。"],
  };
  const svg = $("architecture-svg");
  const taskView = window.parent !== window && new URLSearchParams(location.search).get("mode") === "task";
  const taskNodes = new Map();
  let taskTrace = null;
  const nodes = new Map([...svg.querySelectorAll("g[data-node-id]")].map((node) => [node.dataset.nodeId, node]));
  const byId = new Map(modules.map((module) => [module.id, module]));
  const original = modules.filter((module) => nodes.has(module.id));
  const additions = modules.filter((module) => !nodes.has(module.id));
  const statusSelect = $("status-filter");
  const search = $("module-search");
  const results = $("search-results");
  const detail = $("module-detail");
  const directory = $("module-directory");
  const progress = $("progress-toggle");
  const viewport = $("graph-viewport");
  const art = $("graph-art");
  const inspector = $("details-panel");
  const tooltip = $("node-tooltip");
  const regionSelect = $("region-select");
  const compact = matchMedia("(max-width: 660px)");
  let activeId = null;
  let matching = modules;
  let selectionOrigin = null;
  let lastTooltipNode = null;
  const regions = new Map();
  const regionLabels = new Map([
    ["人类工作入口层：设计平台 / 研发工作台（人类唯一工作入口）", "人类工作入口"],
    ["左翼 · 设计系统对接区", "设计系统"],
    ["右翼 · 验证反馈系统对接区", "验证反馈"],
    ["接入与编排段", "接入与编排"],
    ["Agent 运行时与沙箱执行段", "运行时与沙箱"],
    ["共享服务段", "共享服务"],
    ["缓存服务区（本层核心加速能力）", "缓存服务"],
    ["存储与算力底座", "存储与算力"],
    ["自进化引擎 ⑦：目标 → 评测 → 迭代 → 发布 → 回归", "自进化引擎"],
    ["可观测与任务追踪 ⑧", "可观测与追踪"],
  ]);
  for (const module of original) {
    if (!regions.has(module.group)) {
      const label = regionLabels.get(module.group) ?? module.group;
      regions.set(module.group, { label, modules: [] });
      regionSelect.append(new Option(label, module.group));
    }
    regions.get(module.group).modules.push(module);
  }
  const regionLabel = (group) => regions.get(group)?.label ?? group;

  const nodeEl = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const button = (cls, text, onClick) => {
    const el = nodeEl("button", cls, text);
    el.type = "button";
    el.addEventListener("click", onClick);
    return el;
  };
  const external = (el, href) => {
    el.href = href;
    el.target = "_blank";
    el.rel = "noopener";
    return el;
  };
  const pathPair = (path, label = path) => {
    const pair = nodeEl("div", "path-pair");
    const rawPath = path.replace(/\/$/, "");
    const encoded = rawPath.split("/").map(encodeURIComponent).join("/");
    const isDir = repositoryDirectories.has(path);
    const local = nodeEl("a", "path-local", label);
    local.href = "../" + encoded + (isDir ? "/" : "");
    local.title = "仓库相对路径：" + path;
    const remote = external(nodeEl("a", "path-remote", "GitHub ↗"),
      meta.repository + "/" + (isDir ? "tree" : "blob") + "/" + meta.commit + "/" + encoded);
    remote.setAttribute("aria-label", "在固定快照中查看 " + path);
    if (!taskView) pair.append(local);
    pair.append(remote);
    return pair;
  };
  const badge = (status) => {
    const el = nodeEl("span", "state-badge state-" + status);
    const dot = nodeEl("span", "state-dot");
    dot.setAttribute("aria-hidden", "true");
    el.append(dot, states[status][0]);
    return el;
  };

  const searchKey = (value) => value.normalize("NFKC").toLocaleLowerCase();
  const searchable = new Map(modules.map((module) => [module.id, searchKey([
    module.name, module.id, module.group, module.subtitle, module.current, module.how,
    states[module.status][0], ...module.stack, ...module.code, ...module.tests, ...module.docs,
  ].join(" "))]));

  function moduleButton(module, onClick, showContext = false) {
    const item = button("module-result", undefined, onClick);
    item.dataset.module = module.id;
    item.setAttribute("aria-pressed", String(activeId === module.id));
    const label = nodeEl("span", "", module.name);
    if (showContext && module.current !== module.name) label.append(nodeEl("small", "", module.current));
    item.append(label, badge(module.status));
    return item;
  }

  function detailSection(title, content) {
    const section = nodeEl("div", "module-section");
    section.append(nodeEl("h4", "", title), content);
    return section;
  }
  function detailList(values, empty) {
    if (!values.length) return nodeEl("p", "", empty);
    const list = nodeEl("ul");
    for (const value of values) list.append(nodeEl("li", "", value));
    return list;
  }

  function showDetail(module) {
    inspector.hidden = !module;
    if (!module) {
      detail.replaceChildren();
      $("selection-live").textContent = "";
      return;
    }
    const stack = nodeEl("div", "stack-list");
    for (const name of module.stack) stack.append(nodeEl("span", "stack-tag", name));
    if (!module.stack.length) stack.append(nodeEl("p", "", "尚无独立技术选型。"));
    const evidence = nodeEl("div");
    for (const [key, title] of [["code", "代码或配置"], ["tests", "相关测试"], ["docs", "设计及验证记录"]]) {
      if (!module[key].length) continue;
      const group = nodeEl("div", "code-group");
      group.append(nodeEl("p", "code-group-label", title));
      for (const path of module[key]) group.append(pathPair(path));
      evidence.append(group);
    }
    detail.replaceChildren(
      nodeEl("p", "module-location", module.id),
      badge(module.status),
      nodeEl("h3", "module-name", module.name),
      nodeEl("p", "module-summary", module.current),
      detailSection("技术栈与选型", stack),
      detailSection("如何实现", nodeEl("p", "", module.how)),
      detailSection("已覆盖", detailList(module.done, "暂无该模块的独立实现。")),
      detailSection("剩余缺口", detailList(module.gaps, "按当前审计口径，未发现明确缺口。")),
    );
    if (taskView) {
      const evidence = nodeEl("section", "task-node-evidence");
      detail.prepend(evidence);
      showTaskEvidence(module.id);
    }
    if (module.reviewNote) detail.append(detailSection("验证边界", nodeEl("p", "", module.reviewNote)));
    if (evidence.childElementCount) detail.append(detailSection("实现依据", evidence));
    detail.scrollTop = 0;
    $("detail-context").textContent = (nodes.has(module.id) ? "V1 原图 › " : "") + regionLabel(module.group);
    $("selection-live").textContent = module.name + "，" + states[module.status][0] + "。模块详情已更新。";
    updateModuleNavigation();
  }

  function applySelection() {
    for (const [id, node] of nodes) node.setAttribute("aria-pressed", String(id === activeId));
    for (const control of document.querySelectorAll("button[data-module]")) {
      control.setAttribute("aria-pressed", String(control.dataset.module === activeId));
    }
  }

  function selectModule(id, { locate = false, focus = false, origin } = {}) {
    if (id && origin) selectionOrigin = origin;
    const keepOffset = Boolean(activeId && id);
    activeId = id;
    applySelection();
    hideTooltip();
    showDetail(byId.get(id));
    if (id && locate) locateModule(nodes.get(id));
    else drawCamera(true, true, keepOffset);
    if (focus) detail.focus({ preventScroll: true });
  }

  function closeSelection({ restoreFocus = true } = {}) {
    const selected = nodes.get(activeId);
    const origin = selectionOrigin;
    selectModule(null);
    selectionOrigin = null;
    if (!restoreFocus) return;
    if (origin?.isConnected && origin.getClientRects().length) origin.focus({ preventScroll: true });
    else if (selected && !selected.hasAttribute("data-filtered")) selected.focus({ preventScroll: true });
    else viewport.focus({ preventScroll: true });
  }

  function modulePeers() {
    const current = byId.get(activeId);
    if (!current) return [];
    const peers = (regions.get(current.group)?.modules ?? additions).filter(
      (module) => module.id === activeId || statusSelect.value === "all" || module.status === statusSelect.value);
    return peers;
  }

  function updateModuleNavigation() {
    if (!activeId) return;
    const peers = modulePeers();
    const index = peers.findIndex((module) => module.id === activeId);
    $("previous-module").disabled = index < 1;
    $("next-module").disabled = index >= peers.length - 1;
    $("module-position").textContent = (index + 1) + " / " + peers.length;
  }

  function stepModule(offset) {
    const peers = modulePeers();
    const target = peers[peers.findIndex((module) => module.id === activeId) + offset];
    if (!target) return;
    selectModule(target.id, { locate: true });
    if (document.activeElement === document.body) detail.focus({ preventScroll: true });
  }

  function updateProgress(on) {
    progress.checked = on;
    svg.toggleAttribute("data-progress", on);
  }

  function renderResults() {
    results.hidden = !search.value.trim();
    results.replaceChildren();
    if (results.hidden) return;
    if (!matching.length) results.append(nodeEl("p", "", "没有匹配项。试试模块名、技术栈或代码路径。"));
    for (const module of matching) {
      results.append(moduleButton(module, () => chooseSearchResult(module), true));
    }
  }

  function chooseSearchResult(module) {
    search.value = "";
    refreshMatches();
    toggleDirectory(false);
    selectModule(module.id, { locate: true, focus: true, origin: search });
  }

  function renderDirectory() {
    if (directory.hidden) return;
    const list = $("directory-list");
    const openGroups = new Set([...list.querySelectorAll("details[open]")].map((item) => item.dataset.group));
    list.replaceChildren();
    if (!matching.length) {
      list.append(nodeEl("p", "directory-caption", "当前条件没有匹配模块。"));
      return;
    }
    const groups = new Map();
    for (const module of matching) {
      if (!groups.has(module.group)) groups.set(module.group, []);
      groups.get(module.group).push(module);
    }
    let first = true;
    for (const [group, groupModules] of groups) {
      const section = nodeEl("details", "directory-group");
      section.dataset.group = group;
      section.open = openGroups.has(group) || (first && !openGroups.size) || !!search.value.trim();
      first = false;
      const summary = nodeEl("summary", "", regionLabel(group));
      summary.append(nodeEl("span", "group-count", String(groupModules.length)));
      section.append(summary);
      if (regions.has(group)) {
        section.append(button("directory-location", "在图中定位 ↗", () => {
          toggleDirectory(false);
          locateRegion(group);
          viewport.focus({ preventScroll: true });
        }));
      }
      for (const module of groupModules) {
        section.append(moduleButton(module, () => {
          toggleDirectory(false);
          selectModule(module.id, { locate: true, focus: true, origin: $("directory-toggle") });
        }));
      }
      list.append(section);
    }
  }

  function refreshMatches(showResults = false) {
    const filter = statusSelect.value;
    const terms = searchKey(search.value.trim()).split(/\s+/).filter(Boolean);
    matching = modules.filter((module) =>
      (filter === "all" || filter === module.status) && terms.every((term) => searchable.get(module.id).includes(term)));
    const ids = new Set(matching.map(({ id }) => id));
    for (const [id, node] of nodes) {
      const filtered = !ids.has(id);
      node.toggleAttribute("data-filtered", filtered);
      node.setAttribute("tabindex", filtered ? "-1" : "0");
      node.setAttribute("aria-disabled", String(filtered));
    }
    for (const item of $("status-index").children) {
      item.setAttribute("aria-pressed", String(item.dataset.status === filter));
    }
    const matchingOriginal = matching.filter((module) => nodes.has(module.id)).length;
    const extraCount = matching.length - matchingOriginal;
    $("match-count").textContent = matchingOriginal + " / " + original.length + " 原图" +
      (terms.length && extraCount ? " + " + extraCount + " 新增" : "");
    updateModuleNavigation();
    if (showResults) renderResults();
    else results.hidden = true;
    renderDirectory();
  }

  const { width: diagramWidth, height: diagramHeight } = svg.viewBox.baseVal;
  const workScale = 1.35;
  const maxScale = 3;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const boxes = new Map();
  for (const [id, node] of nodes) {
    const rect = node.querySelector("rect:not(.c-mask)");
    if (!rect) continue;
    const { x, y, width, height } = rect;
    boxes.set(id, { x: x.baseVal.value, y: y.baseVal.value, w: width.baseVal.value, h: height.baseVal.value });
  }
  for (const region of regions.values()) {
    const bounds = region.modules.map(({ id }) => boxes.get(id)).filter(Boolean);
    const x = Math.min(...bounds.map((box) => box.x));
    const y = Math.min(...bounds.map((box) => box.y));
    const right = Math.max(...bounds.map((box) => box.x + box.w));
    const bottom = Math.max(...bounds.map((box) => box.y + box.h));
    region.box = { x, y, w: right - x, h: bottom - y };
  }

  let camera = { x: 0, y: 0, scale: workScale, mode: "work" };
  let rememberedWork = null;
  let requestedRegion = null;
  let offset = { x: 0, y: 0 };
  let lastSize = { w: 0, h: 0 };
  let renderedZoom = "";
  let renderedLocation = "";
  let currentView = null;
  let hasInitializedWork = false;
  const views = ["architecture", "milestones", "roadmap", "evidence"];
  const navLinks = [...document.querySelectorAll(".site-header nav a")];
  const fitScale = () => Math.max(.08, Math.min((viewport.clientWidth - 16) / diagramWidth, (viewport.clientHeight - 16) / diagramHeight, 1));
  const compareToFit = (scale) => Math.max(.06, fitScale() * scale);

  // Exclude the overlaid details bounds without changing the diagram viewport size.
  function visibleArea() {
    const area = { w: viewport.clientWidth, h: viewport.clientHeight };
    if (!activeId || inspector.hidden) return area;
    if (compact.matches) area.h = Math.max(60, inspector.offsetTop);
    else area.w = Math.max(100, inspector.offsetLeft);
    return area;
  }

  function limitCamera() {
    const w = viewport.clientWidth;
    const h = viewport.clientHeight;
    if (camera.mode === "overview") {
      camera.scale = fitScale();
      camera.x = (w - diagramWidth * camera.scale) / 2;
      camera.y = (h - diagramHeight * camera.scale) / 2;
      return;
    }
    const scaledW = diagramWidth * camera.scale;
    const scaledH = diagramHeight * camera.scale;
    camera.x = scaledW <= w ? (w - scaledW) / 2 : clamp(camera.x, w - scaledW - 16, 16);
    camera.y = scaledH <= h ? (h - scaledH) / 2 : clamp(camera.y, h - scaledH - 16, 16);
  }

  function fitSelectedOutsideDrawer(priorOffset) {
    const box = boxes.get(activeId);
    if (!box || inspector.hidden) return { x: 0, y: 0 };
    const area = visibleArea();
    const { x, y, scale } = camera;
    const x1 = x + priorOffset.x + box.x * scale;
    const x2 = x1 + box.w * scale;
    const y1 = y + priorOffset.y + box.y * scale;
    const y2 = y1 + box.h * scale;
    if (compact.matches) {
      if (y2 <= area.h - 10 || y1 > viewport.clientHeight) return priorOffset;
      return { x: 0, y: priorOffset.y + Math.max(10 - y1, area.h - 10 - y2) };
    }
    if (x2 <= area.w - 10 || x1 > viewport.clientWidth) return priorOffset;
    return { x: priorOffset.x + Math.max(10 - x1, area.w - 10 - x2), y: 0 };
  }

  function canvasLocation() {
    if (camera.mode === "overview") return "V1 原图 · 全景";
    if (requestedRegion) return "V1 原图 · " + regionLabel(requestedRegion);
    const area = visibleArea();
    const x = (area.w / 2 - camera.x - offset.x) / camera.scale;
    const y = (area.h / 2 - camera.y - offset.y) / camera.scale;
    let nearest = null;
    let distance = Infinity;
    for (const region of regions.values()) {
      const box = region.box;
      const dx = x - clamp(x, box.x, box.x + box.w);
      const dy = y - clamp(y, box.y, box.y + box.h);
      if (dx * dx + dy * dy < distance) {
        nearest = region;
        distance = dx * dx + dy * dy;
      }
    }
    return "V1 原图 · " + (nearest?.label ?? "工作视图");
  }

  function drawCamera(animate = false, avoidDrawer = true, keepOffset = false) {
    if (!viewport.clientWidth || !viewport.clientHeight) return;
    limitCamera();
    const priorOffset = keepOffset ? offset : { x: 0, y: 0 };
    offset = avoidDrawer ? fitSelectedOutsideDrawer(priorOffset) : priorOffset;
    art.dataset.positioned = "";
    art.classList.toggle("is-travelling", animate);
    art.style.transform = "translate(" + (camera.x + offset.x) + "px," + (camera.y + offset.y) + "px) scale(" + camera.scale + ")";
    const zoomText = Math.round(camera.scale * 100) + "%";
    if (zoomText !== renderedZoom) {
      $("scale-readout").value = zoomText;
      $("zoom-out").disabled = camera.scale <= compareToFit(.8) + .001;
      $("zoom-in").disabled = camera.scale >= maxScale - .001;
      renderedZoom = zoomText;
    }
    $("zoom-work").setAttribute("aria-pressed", String(camera.mode === "work"));
    $("zoom-fit").setAttribute("aria-pressed", String(camera.mode === "overview"));
    const locationText = canvasLocation();
    if (locationText !== renderedLocation) {
      $("canvas-location").textContent = locationText;
      renderedLocation = locationText;
    }
  }

  function centerAt(box, scale = workScale, animate = true) {
    const area = visibleArea();
    camera = {
      x: area.w / 2 - (box.x + box.w / 2) * scale,
      y: area.h / 2 - (box.y + box.h / 2) * scale,
      scale,
      mode: "work",
    };
    hideTooltip();
    drawCamera(animate);
  }

  function resetWork(animate = false) {
    requestedRegion = null;
    centerAt({ x: compact.matches ? 708 : 802, y: 407, w: 0, h: 0 }, workScale, animate);
  }

  function showOverview() {
    if (camera.mode === "work") {
      rememberedWork = {
        x: (viewport.clientWidth / 2 - camera.x) / camera.scale,
        y: (viewport.clientHeight / 2 - camera.y) / camera.scale,
        w: 0, h: 0, scale: camera.scale, region: requestedRegion,
      };
    }
    hideTooltip();
    camera.mode = "overview";
    drawCamera(true);
  }

  function showWork() {
    if (camera.mode === "overview" && rememberedWork && rememberedWork.scale >= 1.15) {
      requestedRegion = rememberedWork.region;
      centerAt(rememberedWork, rememberedWork.scale);
    } else if (boxes.has(activeId)) {
      requestedRegion = byId.get(activeId).group;
      centerAt(boxes.get(activeId));
    } else {
      resetWork(true);
    }
  }

  function locateModule(node) {
    const box = boxes.get(node?.dataset.nodeId);
    if (box) {
      requestedRegion = byId.get(node.dataset.nodeId).group;
      centerAt(box, Math.max(workScale, camera.scale));
    } else drawCamera(true);
  }

  function locateRegion(group) {
    const entry = regions.get(group);
    if (!entry) return;
    closeSelection({ restoreFocus: false });
    requestedRegion = group;
    const box = entry.box;
    const w = viewport.clientWidth;
    const h = viewport.clientHeight;
    const available = Math.min((w - 48) / box.w, (h - 55) / box.h);
    const focus = compact.matches && box.w > w / 1.1 ?
      { ...boxes.get(entry.modules[0].id) } : box;
    centerAt(focus, clamp(available, 1.1, workScale));
  }

  function zoom(next, anchorX, anchorY) {
    requestedRegion = null;
    const area = visibleArea();
    const x = anchorX ?? area.w / 2;
    const y = anchorY ?? area.h / 2;
    const scale = clamp(next, compareToFit(.8), maxScale);
    const ratio = scale / camera.scale;
    camera = {
      x: x - (x - camera.x - offset.x) * ratio,
      y: y - (y - camera.y - offset.y) * ratio,
      scale,
      mode: "work",
    };
    hideTooltip();
    drawCamera(false, false);
  }

  function panBy(x, y) {
    requestedRegion = null;
    camera = { ...camera, x: camera.x + offset.x + x, y: camera.y + offset.y + y, mode: "work" };
    hideTooltip();
    drawCamera(false, false);
  }

  function ensureModuleInView(node) {
    const area = visibleArea();
    const nodeBox = node.getBoundingClientRect();
    const viewBox = viewport.getBoundingClientRect();
    if (nodeBox.left < viewBox.left + 5 || nodeBox.right > viewBox.left + area.w - 5 ||
        nodeBox.top < viewBox.top + 5 || nodeBox.bottom > viewBox.top + area.h - 5) {
      const box = boxes.get(node.dataset.nodeId);
      if (box) {
        requestedRegion = byId.get(node.dataset.nodeId).group;
        centerAt(box, camera.scale, false);
      }
    }
  }

  function hideTooltip(node) {
    if (node && lastTooltipNode !== node) return;
    lastTooltipNode = null;
    tooltip.hidden = true;
  }

  function showTooltip(node) {
    if (node.hasAttribute("data-filtered") || viewport.classList.contains("is-dragging")) return;
    const module = byId.get(node.dataset.nodeId);
    if (!module || activeId === module.id) return;
    lastTooltipNode = node;
    tooltip.replaceChildren(nodeEl("strong", "", module.name), badge(module.status));
    if (module.current !== module.name) tooltip.append(nodeEl("p", "", module.current));
    tooltip.hidden = false;
    const area = visibleArea();
    const box = node.getBoundingClientRect();
    const viewBox = viewport.getBoundingClientRect();
    const height = tooltip.offsetHeight;
    const width = tooltip.offsetWidth;
    const x = clamp(box.left - viewBox.left + box.width / 2 - width / 2, 8, Math.max(8, area.w - width - 8));
    const below = box.bottom - viewBox.top + 7;
    const y = below + height <= area.h - 8 ? below : Math.max(8, box.top - viewBox.top - height - 7);
    tooltip.style.left = x + "px";
    tooltip.style.top = y + "px";
  }

  function showView(requested) {
    const next = !taskView && views.includes(requested) ? requested : "architecture";
    if (currentView === next) return;
    currentView = next;
    for (const view of views) $(view).hidden = view !== next;
    for (const nav of navLinks) {
      if (nav.hash === "#" + next) nav.setAttribute("aria-current", "page");
      else nav.removeAttribute("aria-current");
    }
    if (next === "architecture") {
      if (hasInitializedWork) drawCamera();
      else {
        if (taskView) showOverview();
        else resetWork();
        hasInitializedWork = true;
      }
    } else {
      results.hidden = true;
      toggleDirectory(false);
      hideTooltip();
    }
  }

  function showTaskEvidence(id) {
    const pane = detail.querySelector(".task-node-evidence");
    if (!pane) return;
    const entry = taskNodes.get(id);
    pane.replaceChildren(nodeEl("h4", "", "本次任务"));
    if (!entry) {
      pane.append(nodeEl("p", "", "当前视图没有这个模块的执行事件。"));
      return;
    }
    for (const item of entry.events.slice(-8).reverse()) {
      pane.append(button("task-event", item.context + " · " + item.label + " #" + item.seq, () => {
        window.parent.postMessage({ type: "sca-architecture:event", task: taskTrace.task, seq: item.seq },
          location.origin);
      }));
    }
  }

  function connectTaskTrace() {
    document.body.dataset.taskView = "";
    const namespace = "http://www.w3.org/2000/svg";
    const svgElement = (tag, attrs) => {
      const element = document.createElementNS(namespace, tag);
      for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
      return element;
    };
    const originalEdges = new Map([...svg.querySelectorAll("path[data-edge-from][marker-end]")]
      .map(path => [path.dataset.edgeFrom + ">" + path.dataset.edgeTo, path]));
    // V2 transitions missing from the original wiring. Coordinates use the original viewBox.
    const runtimeRoutes = {
      "ui_goal>ui_dispatch": "M260 127H300",
      "ui_dispatch>task_entry": "M385 158V228H334V300",
      "task_entry>dag": "M382 362V384H485V392",
      "dag>sandbox_sched": "M592 416H612V331H633",
      "sandbox_pool>runtime": "M846 362V376H740V392",
      "sandbox_pool>recover_gate": "M934 362V372H803V515H785",
      "recover_gate>runtime": "M709 484V454",
      "runtime>executor": "M785 423H815",
      "executor>hand_brain": "M891 454V484",
      "executor>checkpoint": "M891 454V472H334V484",
      "executor>dag": "M839 454V462H526V454",
      "checkpoint>sandbox_pool": "M368 546V558H983V351H967",
      "executor>repos": "M967 424H990V844H938V858",
      "ui_quality>ui_confirm": "M780 127H820",
      "planner>ui_approve": "M516 300V237H1190V158",
      "optim_entry>runtime": "M410 607H613V423H633",
      "optim_entry>planner": "M382 576V555H606V330H592",
      "verify_gw>optim_entry": "M1384 351H1361V671H335V638",
      "ui_confirm>ui_history": "M932 158V174H1450V158",
      "evo_goal>evo_eval": "M212 1079H236",
      "evo_eval>evo_improve": "M406 1079H430",
      "evo_improve>evo_release": "M600 1079H624",
      "evo_release>evo_eval": "M710 1110V1121H323V1110",
    };
    const layers = svgElement("g", { class: "task-route-layer", "aria-hidden": "true" });
    svg.insertBefore(layers, nodes.values().next().value);
    const marker = svgElement("marker", {
      id: "task-arrow", markerWidth: 10, markerHeight: 9, refX: 9, refY: 4.5,
      orient: "auto", markerUnits: "userSpaceOnUse",
    });
    marker.append(svgElement("path", { d: "M0 0L10 4.5 0 9Z", class: "task-arrow-head" }));
    svg.querySelector("defs").append(marker);
    let painted = new Set();
    function paint(trace) {
      taskTrace = trace;
      for (const id of painted) nodes.get(id)?.removeAttribute("data-task-state");
      painted = new Set();
      taskNodes.clear();
      for (const entry of trace.nodes) {
        const node = nodes.get(entry.id);
        if (!node) continue;
        painted.add(entry.id);
        taskNodes.set(entry.id, entry);
        node.dataset.taskState = entry.state;
      }
      layers.replaceChildren();
      for (const edge of trace.edges) {
        const original = originalEdges.get(edge.id);
        const d = original?.getAttribute("d") || runtimeRoutes[edge.id];
        if (!d) continue;
        layers.append(svgElement("path", {
          d, "marker-end": "url(#task-arrow)",
          class: "task-route" + (original ? "" : " task-extension"),
          "data-task-state": edge.state, "data-task-edge": edge.id,
        }));
      }
      if (activeId) showTaskEvidence(activeId);
    }
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent || event.origin !== location.origin) return;
      const message = event.data;
      if (message?.type === "sca-architecture:trace" && Array.isArray(message.nodes)
          && Array.isArray(message.edges)) paint(message);
      if (message?.type === "sca-architecture:locate") {
        const node = nodes.get(message.id);
        if (node) locateModule(node);
      }
    });
    const names = Object.fromEntries(original.map(({ id, name }) => [id, name]));
    window.parent.postMessage({ type: "sca-architecture:ready", modules: names }, location.origin);
  }

  function toggleDirectory(on, focus = false) {
    if (on && compact.matches && activeId) closeSelection({ restoreFocus: false });
    directory.hidden = !on;
    $("directory-toggle").setAttribute("aria-expanded", String(on));
    renderDirectory();
    if (focus) (on ? $("close-directory") : $("directory-toggle")).focus();
  }

  for (const [id, node] of nodes) {
    const module = byId.get(id);
    if (!module) continue;
    node.dataset.status = module.status;
    node.querySelector("rect:not(.c-mask)")?.classList.add("arch-hit");
    node.querySelector(":scope > title")?.remove();
    node.setAttribute("aria-label", module.name + "，" + states[module.status][0]);
    node.setAttribute("aria-controls", "module-detail");
    node.addEventListener("click", () => selectModule(id, { origin: node }));
    node.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      if (!node.hasAttribute("data-filtered")) selectModule(id, { focus: true, origin: node });
    });
    node.addEventListener("focus", () => {
      if (!node.matches(":focus-visible")) return;
      ensureModuleInView(node);
      showTooltip(node);
    });
    node.addEventListener("blur", () => hideTooltip(node));
    node.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "touch") showTooltip(node);
    });
    node.addEventListener("pointerleave", () => hideTooltip(node));
  }
  for (const legend of svg.querySelectorAll('[data-legend-kind][role="button"]')) {
    legend.removeAttribute("role");
    legend.removeAttribute("tabindex");
    legend.removeAttribute("aria-pressed");
  }

  $("diagram-total").textContent = original.length + " 个原图模块 · " + additions.length + " 项 V2 新增能力";
  for (const [status, [label, definition]] of Object.entries(states)) {
    const count = original.filter((module) => module.status === status).length;
    statusSelect.append(new Option(label + " · " + count, status));
    const item = button("status-count state-" + status, undefined, () => {
      statusSelect.value = statusSelect.value === status ? "all" : status;
      updateProgress(true);
      refreshMatches();
    });
    item.dataset.status = status;
    item.title = definition;
    const dot = nodeEl("span", "state-dot");
    dot.setAttribute("aria-hidden", "true");
    item.append(dot, label, nodeEl("span", "status-number", String(count)));
    $("status-index").append(item);
    const row = nodeEl("div", "status-def");
    row.append(badge(status), nodeEl("p", "", definition));
    $("status-definitions").append(row);
  }

  progress.addEventListener("change", () => {
    updateProgress(progress.checked);
    if (!progress.checked) {
      statusSelect.value = "all";
      refreshMatches();
    }
  });
  statusSelect.addEventListener("change", () => {
    if (statusSelect.value !== "all") updateProgress(true);
    refreshMatches();
  });
  search.addEventListener("input", () => refreshMatches(true));
  search.addEventListener("focus", renderResults);
  search.addEventListener("keydown", (event) => {
    if (event.isComposing || !search.value.trim()) return;
    if (event.key === "Enter" && matching.length) {
      event.preventDefault();
      chooseSearchResult(matching[0]);
    } else if (event.key === "ArrowDown" && !results.hidden && results.querySelector("button")) {
      event.preventDefault();
      results.querySelector("button").focus();
    }
  });
  search.parentElement.addEventListener("focusout", (event) => {
    if (!search.parentElement.contains(event.relatedTarget)) results.hidden = true;
  });
  document.addEventListener("pointerdown", (event) => {
    if (!search.parentElement.contains(event.target)) results.hidden = true;
  });
  $("directory-toggle").addEventListener("click", () => toggleDirectory(directory.hidden, true));
  $("close-directory").addEventListener("click", () => toggleDirectory(false, true));
  $("clear-selection").addEventListener("click", () => closeSelection());
  $("previous-module").addEventListener("click", () => stepModule(-1));
  $("next-module").addEventListener("click", () => stepModule(1));
  $("zoom-work").addEventListener("click", showWork);
  $("zoom-fit").addEventListener("click", showOverview);
  $("zoom-out").addEventListener("click", () => zoom(camera.scale / 1.2));
  $("zoom-in").addEventListener("click", () => zoom(camera.scale * 1.2));
  $("zoom-native").addEventListener("click", () => zoom(1));
  regionSelect.addEventListener("change", () => {
    locateRegion(regionSelect.value);
    regionSelect.value = "";
  });
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
  viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
    if (event.shiftKey) {
      panBy(-(event.deltaX || event.deltaY) * unit, 0);
      return;
    }
    const bounds = viewport.getBoundingClientRect();
    const delta = clamp(event.deltaY * unit, -120, 120);
    zoom(camera.scale * Math.exp(-delta * .003), event.clientX - bounds.left, event.clientY - bounds.top);
  }, { passive: false });

  const pointers = new Map();
  let gesture = null;
  let suppressClickUntil = 0;
  function pinchPose() {
    const [first, second] = pointers.values();
    const bounds = viewport.getBoundingClientRect();
    return {
      x: (first.x + second.x) / 2 - bounds.left,
      y: (first.y + second.y) / 2 - bounds.top,
      distance: Math.max(1, Math.hypot(first.x - second.x, first.y - second.y)),
    };
  }
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || pointers.size >= 2 ||
        (event.pointerType === "mouse" && event.target.closest("[data-node-id]"))) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2) {
      gesture = { mode: "pinch", pose: pinchPose(), moved: true };
      for (const id of pointers.keys()) viewport.setPointerCapture(id);
    } else {
      gesture = { mode: "pan", x: event.clientX, y: event.clientY, moved: false };
      if (event.pointerType === "mouse") viewport.setPointerCapture(event.pointerId);
    }
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!gesture || !pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (gesture.mode === "pinch") {
      const pose = pinchPose();
      const previous = gesture.pose;
      zoom(camera.scale * pose.distance / previous.distance, previous.x, previous.y);
      panBy(pose.x - previous.x, pose.y - previous.y);
      gesture.pose = pose;
      return;
    }
    const x = event.clientX - gesture.x;
    const y = event.clientY - gesture.y;
    if (!gesture.moved && Math.hypot(x, y) < 5) return;
    gesture.moved = true;
    viewport.classList.add("is-dragging");
    if (!viewport.hasPointerCapture(event.pointerId)) viewport.setPointerCapture(event.pointerId);
    panBy(x, y);
    gesture.x = event.clientX;
    gesture.y = event.clientY;
  });
  function endPointer(event) {
    if (!pointers.delete(event.pointerId)) return;
    if (gesture?.moved) suppressClickUntil = performance.now() + 160;
    if (pointers.size) {
      const [remaining] = pointers.values();
      gesture = { mode: "pan", x: remaining.x, y: remaining.y, moved: true };
    } else {
      gesture = null;
      viewport.classList.remove("is-dragging");
    }
  }
  viewport.addEventListener("pointerup", endPointer);
  viewport.addEventListener("pointercancel", endPointer);
  viewport.addEventListener("lostpointercapture", (event) => {
    if (event.target === viewport) endPointer(event);
  });
  viewport.addEventListener("click", (event) => {
    if (event.detail && performance.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  new ResizeObserver(() => {
    const size = { w: viewport.clientWidth, h: viewport.clientHeight };
    if (!size.w || !size.h) return;
    if (camera.mode === "work" && lastSize.w) {
      camera.x += (size.w - lastSize.w) / 2;
      camera.y += (size.h - lastSize.h) / 2;
    }
    lastSize = size;
    hideTooltip();
    drawCamera();
  }).observe(viewport);

  document.addEventListener("keydown", (event) => {
    if (currentView !== "architecture") return;
    if (event.key === "Escape") {
      if (!results.hidden) {
        search.focus();
        results.hidden = true;
      } else if (!directory.hidden) toggleDirectory(false, true);
      else if (activeId) closeSelection();
      else hideTooltip();
      return;
    }
    const editable = event.target.closest("input, select, textarea, [contenteditable]");
    if (editable || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "/") {
      event.preventDefault();
      search.focus();
    } else if (viewport.contains(event.target)) {
      const step = event.shiftKey ? 180 : 65;
      const directions = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (event.key in directions) {
        event.preventDefault();
        panBy(...directions[event.key]);
      } else if (["+", "=", "-", "0"].includes(event.key)) {
        event.preventDefault();
        if (event.key === "0") showOverview();
        else zoom(camera.scale * (event.key === "-" ? 1 / 1.2 : 1.2));
      }
    }
  });

  $("snapshot-date").textContent = meta.date;
  $("snapshot-date").dateTime = meta.date;
  const snapshotCommit = $("snapshot-commit");
  snapshotCommit.textContent = meta.commit.slice(0, 7);
  snapshotCommit.title = meta.commit;
  external(snapshotCommit, meta.repository + "/commit/" + meta.commit);
  external($("source-link"), meta.repository);
  $("audit-method").textContent = meta.method;
  $("audit-scope").textContent = meta.scope.trim().replace(/[。.]$/, "") + "。提交 SHA：" + meta.commit + "。";
  if (Array.isArray(meta.evidenceReport) && meta.evidenceReport.length) {
    $("audit-report").append(...meta.evidenceReport.map((note) => nodeEl("li", "", note)));
    $("audit-report").hidden = false;
  }
  for (const path of ["docs/archive/architecture-v1.md", "docs/architecture.md",
    "docs/design-phase2b.md", "docs/design-phase3.md", "docs/design-phase4.md",
    "Milestone/main-flow-and-next.md", "Milestone/portal-demo.md", "docs/handoff.md"]) {
    $("evidence-links").append(pathPair(path));
  }

  const phases = [
    {
      id: "01 / MVP", title: "一期 · 端到端跑通", source: "docs/design.md",
      summary: "提交编码任务，沙箱中运行 Codex、验证和自动修复，推送结果后由人工确认或提出修改意见。",
      scope: "架构文档记录真实 Temporal、Postgres、Docker、Codex 的端到端流程通过，包含人工再优化。",
    },
    {
      id: "02 / HARDENING", title: "二期 · 组件加固", source: "docs/design-phase2.md",
      summary: "加固沙箱、凭证、身份、编排、存储、验证、可观测、加速和并行九项能力。",
      scope: "阶段验收以单机环境为主；多节点、真实 CI、运维后端等验证移入二期收尾。",
      items: [
        "沙箱与凭证：Docker / Kubernetes 驱动、出站白名单、每轮模型临时凭证。",
        "身份与编排：OIDC、租户行级隔离、配额、暂停恢复及 Worker 续跑。",
        "存储与验证：S3、事件归档、并行验证及 CI 回调接口。",
        "观测、加速与并行：追踪、预算、仓库与依赖缓存、显式子任务并行。",
      ],
    },
    {
      id: "02B / B1–B9", title: "二期收尾 · 验证跨节点与运维", source: "docs/design-phase2b.md",
      summary: "二期收尾交付状态同步、集群隔离、真实 CI、受限权限、费用和维护流程。",
      scope: "交接记录与 B4 补记包括 GitHub Runner、Gitea 和真实私有 GitHub App 的通过结果；Prometheus → Alertmanager → 本地 webhook 已验证，真实 Slack / 邮件渠道和集群 Worker 实际部署仍待验证。",
      items: [
        "B1 多节点：对象存储同步与换 Worker 续跑；Worker 集群部署另待实测。",
        "B2 集群网络隔离：kind / Calico 隔离用例。",
        "B3 真实 CI：Gitea 与 GitHub Runner 路径。",
        "B4 仓库凭证：Gitea + OpenBao 与真实私有 GitHub App 均有结果；App 仅安装在一个私有测试仓库，平台只推送分支，不创建或更新 PR。",
        "B5 权限：租户角色与 CI 受限令牌。",
        "B6 金额预算：LiteLLM 用量、成本落库和超预算停止。",
        "B7 运维：追踪、告警、定期归档、缓存淘汰和孤儿清理；本地 webhook 已验证，真实 Slack / 邮件渠道未验证。",
        "B8 自动拆分：检查已跟踪文件上的规划路径重叠，支持并行合并和单 Agent 回退；执行期不强制规划路径边界。",
        "B9 启动时延评估：在两节点 kind 测量冷启动并实现固定大小预热池。",
      ],
    },
    {
      id: "03 / C1–C4", title: "三期 · 扩展平台能力", source: "docs/design-phase3.md",
      summary: "把内核与场景分开，增加分析、设计意图、项目记忆与回归评测。",
      scope: "数据分析有脚本化端到端；真实分析模型和 embeddings 待验。首批编码评测能跑通，但两个模型均 12/12，尚无区分度。",
      items: [
        "C1 第二个场景：内核抽取、文件输入、分析复跑校验和制品下载。",
        "C2 设计意图：OpenAPI、参考截图和规则文件编译。",
        "C3 记忆与知识库：从审阅意见提炼，经人工确认后注入后续任务。",
        "C4 评测：隐藏验收、成组运行、变体比较和回归闸门。",
      ],
    },
    {
      id: "04 / P4.1–P4.6", title: "四期 · 脑手分离与并行加固", source: "docs/design-phase4.md",
      summary: "交付 Portal 评测→诊断→修复→构建→回归，增加 brain 池、轨迹评分、pi、准入、限流及可选本地工作区优化。",
      scope: "负责人现场验证：overlay 闭环 5/6 → 6/6；pi 与限流各 6/6；kind TLS 23 passed。最新指定基线记录 491 passed、1 skipped；全量回归首轮 704 passed、5 failed 后有逐项修复，未记录最终全量全绿统计。",
      items: [
        "P4.1 / P4.1b：无网络、无模型 key 的 hand；warm p50 357 ms、RSS 约 374 MiB。池会话需 Worker 亲和，长期内存稳定性待验。",
        "K 轨迹检查：验收与过程分别判定；绕开工具缺陷也可判失败，支持历史事件重评。",
        "P4.3：只读依赖与 overlay；Portal 真实闭环通过，评测 upper 约 86 KB。仓库相对路径 intent 待接。",
        "TLS 出口：Docker 21 项通过；K8s CA 的 kind 23 项通过，多副本 RWX CA PVC 未验证，K8s 模型网关仍未接通。",
        "P 小修复：评测预建行与启动失败标记、迁移 0013 权限、Docker 可执行路径、brain 取消进程组；负责人实测确认。",
        "pi 1.5.2：真实模型 6/6；不提供为 Portal 的 OpenCode 源码修复后端。",
        "P4.5：父任务准入、优先级、加权排序与网关反压；limiter 真实 45 调用、6.4 秒排队、0 次上游 429。单副本限制仍在。",
        "P4.2：冻结后 CPU 降到 0%，内存仍 60.7 MiB，不满足省内存退出条件；实际小套件活跃率 0.80–0.97。",
        "P4.4：两个 Docker 测试与带 setup 分叉通过；fork 0.07 秒，进入 RUNNING 2.8 → 1.6 秒。无 setup 的 12/12 评测没有分叉，Best-of-N 未实现。",
        "P4.6：provider handd 基准已验，串行 p50 为 2.0–7.6 ms；brain driver 路由尚未接入。",
        "默认关闭：复制层、overlay、快照、limiter、空闲冻结及 handd。稳定性维度尚未完成，单次闭环不能证明稳定性。",
      ],
    },
  ];
  const roadmap = [
    {
      priority: "P0", title: "把 handd 接入 brain 工具路径",
      next: "provider 的长连接已有真实容器和 500 次/组基准，brain driver 仍逐次 docker exec；接入共享 relay 并保持取消与冻结互斥。",
      exit: "在脑手分离真实任务中确认工具经过 handd，重新量 p50/p99 与完整 CPU，并保持无 key hand 和取消清理。",
      modules: ["handd", "brain_split", "idle_suspend"],
    },
    {
      priority: "P0", title: "完成稳定性维度与困难评测",
      next: "Portal 已展示重复一致性；稳定性维度尚未完成。扩充真实缺陷和多次重复，区分 Agent 绕行、真实修复与随机波动。",
      exit: "报告重复次数、方差、失败证据及回归；单次 83% → 100% 不作稳定性结论。",
      modules: ["portal_loop", "trajectory_checks", "evo_eval"],
    },
    {
      priority: "P1", title: "验证 K8s 多副本 CA 与模型闭环",
      next: "保留 kind TLS 23 passed 的已验结果，验证共享 RWX CA PVC、flock、轮换与 Worker 部署；另接仍被 factory 拒绝的 K8s 模型网关。",
      exit: "多副本共用 CA、排空轮换后信任正确；实际部署 Worker 验证 RBAC/探针/排空/伸缩，沙箱经临时 key 完成真实模型任务。",
      modules: ["k8s", "net_sec", "model_gateway"],
    },
    {
      priority: "P1", title: "补齐快照分叉与 overlay 边界",
      next: "快照已验证带 setup 的分叉，未实现 Best-of-N；补 CodingSpec 顶层字段、聚合预算与 API，处理 overlay 的仓库相对路径 intent。",
      exit: "真实重复任务实际命中快照、结果一致；Best-of-N 有明确预算和汇总，intent 在隔离边界内读取。",
      modules: ["workspace_forks", "workspace_overlay", "design_in"],
    },
    {
      priority: "P1", title: "补齐多 Worker 会话与容量验证",
      next: "常驻池仍需 Worker 亲和，父任务限槽不限制所有子沙箱。limiter 单副本且一键 setup 不部署，开关仍默认关闭。",
      exit: "验证按任务会话导出/导入、长期 RSS、子任务资源上限及压力文件联调；多副本限流先提供共享桶。",
      modules: ["brain_pool", "admission_control", "gateway_limiter"],
    },
    {
      priority: "P1", title: "验证真实分析、embeddings 与通知渠道",
      next: "历史已有脚本化分析、pgvector 假向量与本地告警 webhook；补真实模型分析、embeddings 和 Slack/邮件投递。",
      exit: "保存脱敏输入、模型及花费、重跑结果和真实渠道收件证据；不以已有 fake/本地接收器代替。",
      modules: ["analysis_kind", "vector_store", "obs_store"],
    },
    {
      priority: "P2", title: "按实测收益启用可选优化",
      next: "overlay、复制层、快照、limiter、冻结与 handd 默认关闭；冻结只省 CPU，当前小套件几乎没有收益。",
      exit: "按部署条件逐项验证后启用，记录收益及回退方法；冻结不得宣称省内存或增加同等内存密度。",
      modules: ["idle_suspend", "workspace_overlay", "workspace_forks", "gateway_limiter"],
    },
  ];
  for (const phase of phases) {
    const card = nodeEl("article", "phase-card");
    card.append(nodeEl("div", "card-kicker", phase.id), nodeEl("h3", "", phase.title),
      nodeEl("p", "", phase.summary), nodeEl("p", "", "验证边界：" + phase.scope));
    if (phase.items) {
      const items = nodeEl("details");
      items.append(nodeEl("summary", "", "查看交付项 · " + phase.items.length), detailList(phase.items, ""));
      card.append(items);
    }
    card.append(pathPair(phase.source, "阶段设计与验证记录"));
    $("phase-list").append(card);
  }
  for (const item of roadmap) {
    const card = nodeEl("article", "roadmap-card roadmap-" + item.priority.toLowerCase());
    const references = nodeEl("div", "roadmap-references");
    for (const id of item.modules) {
      const module = byId.get(id);
      if (!module) continue;
      references.append(button("roadmap-reference", module.name, () => {
        location.hash = "architecture";
        showView("architecture");
        search.value = "";
        if (statusSelect.value !== module.status) statusSelect.value = "all";
        refreshMatches();
        selectModule(id, { locate: true, focus: true, origin: viewport });
      }));
    }
    card.append(nodeEl("div", "card-kicker", item.priority + " / 建议优先级"),
      nodeEl("h3", "", item.title), nodeEl("p", "", item.next),
      nodeEl("p", "", "退出条件：" + item.exit), references);
    $("roadmap-list").append(card);
  }

  if (taskView) connectTaskTrace();
  showView(location.hash.slice(1));
  updateProgress(false);
  statusSelect.value = "all";
  refreshMatches();
})();
