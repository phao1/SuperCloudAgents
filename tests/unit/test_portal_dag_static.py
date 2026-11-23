import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_task_dag import history, task

from sca.core.models import TaskState
from sca.core.task_dag import build_task_dag
from scripts.dag_demo import GRAPH

STATIC = Path("src/sca/api/static")


def run_node(script, graph=None):
    node = shutil.which("node") or str(Path.home() / ".local/bin/node")
    if not Path(node).exists():
        pytest.skip("Node is unavailable")
    subprocess.run([node, "-e", script, str((STATIC / "task-dag.js").resolve())],
                   input=json.dumps(graph), text=True, check=True, timeout=10)


@pytest.fixture
def demo_dag():
    owner = task(TaskState.SUCCEEDED, spec={"plan": "auto"})
    subtasks = [{"name": name, "depends_on": list(needs),
                 **({"best_of": {"n": 3}} if name == "fx" else {})} for name, needs in GRAPH.items()]
    rows = [("plan.started", {"attempt": 1}), ("agent.turn_started", {"subtask": "plan-1", "turn": 0}),
            ("agent.turn_completed", {"subtask": "plan-1", "turn": 0}),
            ("plan.completed", {"attempt": 1, "split": True, "subtasks": subtasks})]

    def run(turn, passed=True, **scope):
        payload = {**scope, "turn": turn}
        rows.extend([
            ("agent.turn_started", payload), ("agent.tool", {**payload, "name": "edit"}),
            ("agent.turn_completed", {**payload, "cost_usd": .001}),
            ("verification.started", payload), ("verification.completed", {**payload, "passed": passed}),
        ])

    for name in GRAPH:
        rows.append(("subtask.started", {"subtask": name}))
        if name == "fx":
            for attempt in range(1, 4):
                run(1, subtask=name, best_of_attempt=attempt)
                rows.append(("best_of.attempt_completed", {"subtask": name, "attempt": attempt,
                                                           "verification": {"passed": True}}))
            rows.append(("best_of.selected", {"subtask": name, "attempt": 2}))
        else:
            run(1, name != "pricing", subtask=name)
            if name == "pricing":
                run(2, subtask=name)
    rows.append(("subtasks.merged", {"subtasks": [{"name": name, "status": "merged"} for name in GRAPH]}))
    rows.extend([("verification.completed", {"turn": 1, "passed": True}), ("review.requested", {"turn": 1}),
                 ("user.message", {"message_id": "m", "text": "确认金额"}),
                 ("user.message.delivered", {"message_id": "m", "turn": 2})])
    run(2)
    rows.extend([("user.message.applied", {"message_id": "m", "turn": 2}),
                 ("ci.completed", {"turn": 2, "passed": True}), ("pr.opened", {"number": 1}),
                 ("review.requested", {"turn": 2}), ("review.submitted", {"decision": "approve"})])
    return build_task_dag(owner, history(owner, *rows), []).model_dump(mode="json", by_alias=True)


def test_task_dag_panel_uses_local_assets_and_the_existing_stream():
    page = (STATIC / "portal.html").read_text()
    panel = page[page.index('id="task-dag"'):page.index('id="swimlane"')]
    assert "执行图 DAG" in panel
    assert 'aria-label="节点详情"' in panel
    assert 'class="dag-fit"' in panel
    assert 'data-dag-orientation="vertical" type="button" aria-pressed="true"' in panel
    assert 'data-dag-orientation="horizontal" type="button" aria-pressed="false"' in panel
    assert '<script src="/portal/assets/task-dag.js"></script>' in page
    assert '<link rel="stylesheet" href="/portal/assets/task-dag.css">' in page
    stream = page[page.index("async function streamTask"):page.index("function renderTimeline")]
    assert "refreshTaskDag(taskId,true)" in stream
    assert "taskId=>data('/tasks/'+enc(taskId)+'/dag')" in page
    assert "refreshTaskDag(id);" in page
    script = (STATIC / "task-dag.js").read_text()
    assert "innerHTML" not in script
    assert "setPointerCapture" in script and "ResizeObserver" in script
    assert "'wheel'" in script and "'keydown'" in script
    assert "encodeURIComponent(childId)" in script
    css = (STATIC / "task-dag.css").read_text()
    assert "prefers-reduced-motion" in css and "light-dark(" in css
    assert "max-width: 550px" in css and "80vh" in css
    for status in ("pending", "running", "succeeded", "failed", "cancelled", "skipped"):
        assert f"{status}:" in script
    for kind in ("sequence", "dependency", "spawn", "select", "merge"):
        assert f".dag-edge-{kind}" in css


def test_subtask_peers_stay_in_one_level_when_one_prerequisite_is_expanded():
    run_node("""
const assert = require('node:assert/strict');
const {layout, projectGraph} = require(process.argv[1]);
const lane = (id, level, column) => ({id, kind: 'subtask', task_id: 'task', meta: {level, lane: column}});
const dag = {
  nodes: [lane('a', 0, 0), lane('b', 0, 1), lane('c', 1, 0), lane('d', 1, 1),
    {id: 'turn', kind: 'turn', meta: {parent: 'a'}},
  ],
  edges: [{from: 'a', to: 'turn', kind: 'sequence'}, {from: 'turn', to: 'c', kind: 'sequence'},
    {from: 'a', to: 'c', kind: 'dependency'}, {from: 'b', to: 'd', kind: 'dependency'}],
};
for (const direction of ['vertical', 'horizontal']) {
  const positions = Object.fromEntries(layout(projectGraph(dag, new Set(['a'])), direction)
    .nodes.map(item => [item.node.id, item]));
  const along = direction === 'vertical' ? 'y' : 'x';
  assert.equal(positions.a[along], positions.b[along]);
  assert.equal(positions.c[along], positions.d[along]);
  assert(positions.turn[along] < positions.c[along]);
}
""")


def test_dag_layout_layers_branches_without_overlap_and_routes_long_edges():
    run_node("""
const assert = require('node:assert/strict');
const {layout} = require(process.argv[1]);
const dag = {
  nodes: ['end', 'b', 'root', 'a', 'c', 'd', 'join'].map(id => ({id})),
  edges: [
    {from: 'root', to: 'a', kind: 'spawn'}, {from: 'root', to: 'b', kind: 'spawn'},
    {from: 'a', to: 'd', kind: 'dependency'}, {from: 'b', to: 'c', kind: 'dependency'},
    {from: 'b', to: 'd', kind: 'dependency'},
    {from: 'c', to: 'join', kind: 'merge'}, {from: 'd', to: 'join', kind: 'merge'},
    {from: 'root', to: 'join', kind: 'sequence'},
    {from: 'join', to: 'end', kind: 'merge'},
  ],
};
for (const [direction, along, across, length] of [
  [undefined, 'y', 'x', 'height'], ['horizontal', 'x', 'y', 'width'],
]) {
  const result = layout(dag, direction);
  const positions = Object.fromEntries(result.nodes.map(n => [n.node.id, n]));
  assert.equal(result.edges.length, dag.edges.length);
  assert.equal(positions.a[along], positions.b[along]);
  assert.equal(positions.c[along], positions.d[along]);
  assert.notEqual(positions.a[across], positions.b[across]);
  assert.equal(Math.sign(positions.a[across]-positions.b[across]),
               Math.sign(positions.d[across]-positions.c[across]));
  assert(result.edges.every(e => e.source[along] + e.source[length] < e.target[along]));
  for (const a of result.nodes) {
    assert(a.width >= 140);
    for (const b of result.nodes.filter(n => n !== a)) {
      assert(a.x+a.width <= b.x || b.x+b.width <= a.x || a.y+a.height <= b.y || b.y+b.height <= a.y);
    }
  }
  const skip = result.edges.find(e => e.source.node.id === 'root' && e.target.node.id === 'join');
  assert.equal(skip.route.length, 4);
  for (const waypoint of skip.route.slice(1,-1)) {
    assert(result.nodes.filter(n => n.layerStart === waypoint.layerStart).every(n =>
      Math.abs(n.cross-waypoint.cross) > n[direction ? 'height' : 'width']/2));
  }
}
""")


def test_demo_dag_groups_execution_and_keeps_milestones(demo_dag):
    run_node("""
const assert = require('node:assert/strict');
const {layout, projectGraph} = require(process.argv[1]);
const dag = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const folded = projectGraph(dag);
const kind = name => folded.nodes.filter(n => n.kind === name);
assert(dag.nodes.length >= 55);
assert(folded.nodes.length <= 23);
assert.equal(kind('subtask').length, 8);
assert.equal(kind('attempt').length, 3);
for (const name of ['plan', 'interjection', 'review', 'ci', 'pr', 'finish']) assert(kind(name).length, name);
assert.equal(kind('turn').length + kind('tool_group').length + kind('verification').length + kind('fix').length, 0);
const byLabel = label => folded.nodes.find(n => n.label === label);
const pricing = byLabel('pricing'), fx = byLabel('fx'), candidate = byLabel('候选 #2');
const main = kind('execution').find(n => n.summary.turns === 1);
assert.match(main.label, /^主任务执行/);
assert.deepEqual(pricing.summary, {turns: 2, tools: 2, verification: 2});
assert.deepEqual(fx.summary, {turns: 3, tools: 3, verification: 3});
assert.deepEqual(candidate.summary, {turns: 1, tools: 1, verification: 1});
assert.equal(pricing.cost_usd, .002);
assert(pricing.duration_s > 0 && main.duration_s > 0);
assert.equal(pricing.status, 'succeeded');
assert(folded.edges.some(e => e.from === fx.id && e.to === pricing.id && e.kind === 'dependency'));
assert(folded.edges.some(e => e.from === candidate.id && e.kind === 'select'));
const open = projectGraph(dag, new Set([pricing.id, candidate.id, main.id]));
assert.equal(open.nodes.filter(n => n.kind === 'fix').length, 1);
assert.equal(open.nodes.filter(n => n.meta?.parent === pricing.id).length, 6);
assert.equal(open.nodes.filter(n => n.meta?.parent === candidate.id).length, 3);
assert(open.edges.some(e => e.from === main.id && e.kind === 'sequence'));
const check = open.nodes.find(n => n.kind === 'verification' && n.meta?.parent === candidate.id);
assert(open.edges.some(e => e.from === check.id && e.kind === 'select'));
assert.equal(layout(folded).nodes.length, folded.nodes.length);
assert.equal(layout(open).nodes.length, open.nodes.length);
""", demo_dag)


def test_contraction_keeps_a_visible_interjection_on_the_fix_path():
    run_node("""
const assert = require('node:assert/strict');
const {layout, projectGraph} = require(process.argv[1]);
const dag = {
  nodes: [
    {id: 'root', kind: 'task'}, {id: 'verify', kind: 'verification', meta: {parent: 'root'}},
    {id: 'message', kind: 'interjection'}, {id: 'fix', kind: 'fix', meta: {parent: 'root'}},
    {id: 'finish', kind: 'finish'},
  ],
  edges: [ ['root', 'verify'], ['verify', 'fix'], ['verify', 'message'],
    ['message', 'fix'], ['fix', 'finish'],
  ].map(([from, to]) => ({from, to, kind: 'sequence'})),
};
const folded = projectGraph(dag);
assert.deepEqual(layout(folded).nodes.map(n => n.node.id),
  ['root', 'verify/execution', 'message', 'fix/execution', 'finish']);
""")


def test_evolution_dag_folds_cases_without_losing_paths_to_decisions():
    run_node("""
const assert = require('node:assert/strict');
const {layout, projectEvolution} = require(process.argv[1]);
const dag = {
  nodes: [
    {id: 'candidate', kind: 'candidate'}, {id: 'dev', kind: 'eval_run', score: .5, cost_usd: .02},
    {id: 'case1', kind: 'case_task', meta: {parent: 'dev'}},
    {id: 'case2', kind: 'case_task', meta: {parent: 'dev'}},
    {id: 'holdout', kind: 'eval_run'}, {id: 'holdout-case', kind: 'case_task', meta: {parent: 'holdout'}},
    {id: 'decision', kind: 'promotion'},
  ],
  edges: [
    ['candidate', 'dev'], ['dev', 'case1'], ['dev', 'case2'], ['case1', 'holdout'], ['case2', 'holdout'],
    ['holdout', 'holdout-case'], ['holdout-case', 'decision'],
  ].map(([from, to]) => ({from, to, kind: 'sequence'})),
};
const folded = projectEvolution(dag);
assert.deepEqual(folded.nodes.map(node => node.id), ['candidate', 'dev', 'holdout', 'decision']);
assert.deepEqual(folded.edges.map(e => [e.from, e.to]),
  [['candidate', 'dev'], ['dev', 'holdout'], ['holdout', 'decision']]);
assert.deepEqual(folded.nodes[1], {...dag.nodes[1], caseCount: 2, collapsible: true, expanded: false});
const opened = projectEvolution(dag, new Set(['dev']));
assert(opened.nodes.find(node => node.id === 'dev').expanded);
assert.deepEqual(opened.nodes.filter(node => node.kind === 'case_task').map(node => node.id), ['case1', 'case2']);
assert.equal(layout(folded).nodes.length, 4);
const graph = layout(opened);
assert.equal(graph.nodes.length, opened.nodes.length);
assert(graph.edges.every(e => e.source.y < e.target.y));
""")


def test_stream_refresh_keeps_open_groups_and_the_pan_and_zoom(demo_dag):
    run_node("""
const assert = require('node:assert/strict');
const {create} = require(process.argv[1]);
const dag = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
function element() {
  return {
    attrs: new Map(), children: [], listeners: {},
    style: {setProperty() {}, removeProperty() {}},
    setAttribute(key, value) { this.attrs.set(key, String(value)); },
    getAttribute(key) { return this.attrs.get(key); },
    append(...nodes) { this.children.push(...nodes); },
    prepend(...nodes) { this.children.unshift(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(event, listener) { this.listeners[event] = listener; },
    querySelector() { return null; },
    closest() { return this; },
  };
}
const svg = Object.assign(element(), {clientWidth: 1000, clientHeight: 700});
const world = element(), up = element(), fit = element(), scrollHint = element();
const vertical = {...element(), dataset: {dagOrientation: 'vertical'}};
const horizontal = {...element(), dataset: {dagOrientation: 'horizontal'}};
const elements = {'svg': svg, '.dag-load-status': element(), '.dag-detail': element(),
  '.dag-pan-hint': scrollHint, '.dag-zoom-in': up, '.dag-zoom-out': element(), '.dag-fit': fit};
svg.querySelector = () => world;
global.document = {createElement: element, createElementNS: element};
global.ResizeObserver = class { observe() {} };
let scheduled;
global.setTimeout = callback => (scheduled = callback, 1);
global.clearTimeout = () => {};
const controller = create({querySelector: key => elements[key], querySelectorAll: () => [vertical, horizontal]},
  async () => dag);
const taskId = dag.nodes[0].task_id;
const currency = dag.nodes.find(n => n.label === 'currency');
const rendered = id => world.children[1].children.find(n => n.getAttribute('data-node') === id);
const camera = () => world.getAttribute('transform');
const toggle = id => svg.listeners.click({target: rendered(id), detail: 1});
(async () => {
  controller.refresh(taskId);
  await scheduled();
  assert.equal(rendered(currency.id).getAttribute('aria-expanded'), 'false');
  assert.match(camera(), /scale\\(0\\.85\\)$/);
  assert.equal(scrollHint.hidden, false);
  toggle(currency.id);
  up.onclick();
  const beforePan = camera();
  svg.listeners.keydown({key: 'ArrowDown', target: svg, preventDefault() {}});
  const view = camera();
  assert.notEqual(view, beforePan);
  currency.status = 'running';
  controller.refresh(taskId, true);
  await scheduled();
  assert.equal(rendered(currency.id).getAttribute('aria-expanded'), 'true');
  assert.equal(rendered(currency.id).getAttribute('data-status'), 'running');
  assert.equal(camera(), view);
  horizontal.onclick();
  assert.equal(horizontal.getAttribute('aria-pressed'), 'true');
  assert.equal(vertical.getAttribute('aria-pressed'), 'false');
  fit.onclick();
  assert(Number(camera().match(/scale\\(([^)]+)/)[1]) < .85);
  toggle(currency.id);
  assert.equal(rendered(currency.id).getAttribute('aria-expanded'), 'false');
  const turn = dag.nodes.find(n => n.meta.parent === currency.id);
  assert.equal(rendered(turn.id), undefined);
  assert(controller.focus(turn.id));
  assert.equal(rendered(currency.id).getAttribute('aria-expanded'), 'true');
  assert.equal(rendered(turn.id).getAttribute('aria-pressed'), 'true');
})().catch(error => { console.error(error); process.exitCode = 1; });
""", demo_dag)


def test_evolution_bands_keep_each_candidate_with_its_evaluations():
    run_node("""
const assert = require('node:assert/strict');
const {layoutEvolution, projectEvolution, highlightedPath} = require(process.argv[1]);
const node = (id, kind, band, level, column, extra = {}) =>
  ({id, kind, status: 'succeeded', meta: {band, level, column, ...extra}});
const dag = {nodes: [
  node('start', 'evolution', 0, 0, 0), node('base', 'eval_run', 0, 1, 1),
  node('gen', 'generation', 1, 0, 0), node('a', 'candidate', 1, 1, 1), node('b', 'candidate', 1, 1, 2),
  node('devA', 'eval_run', 1, 2, 1, {cases_passed: 1}), node('devB', 'eval_run', 1, 2, 2),
  node('case', 'case_task', 1, 3, 1, {parent: 'devA'}), node('holdoutB', 'eval_run', 1, 4, 2),
  node('gate', 'promotion', 1, 6, 0, {winner_column: 2}),
  node('winner', 'champion', 2, 0, 0, {band_title: '冠军', best: true}),
], edges: [
  ['start', 'base'], ['base', 'gen'], ['gen', 'a'], ['gen', 'b'], ['a', 'devA'], ['b', 'devB'],
  ['devA', 'case'], ['case', 'gate'], ['devB', 'holdoutB'], ['holdoutB', 'gate'], ['gate', 'winner'],
].map(([from, to]) => ({from, to, kind: 'sequence'}))};
const folded = projectEvolution(dag);
assert.equal(folded.nodes.find(node => node.id === 'devA').casePassed, 1);
const result = layoutEvolution(folded);
const pos = Object.fromEntries(result.nodes.map(n => [n.node.id, n]));
assert.deepEqual(result.bands.map(b => b.title), ['基线','第 1 代','冠军']);
assert.equal(pos.a.y, pos.b.y);
assert.equal(pos.devA.y, pos.devB.y);
assert.equal(pos.a.x, pos.devA.x);
assert.equal(pos.holdoutB.x, pos.b.x);
assert(pos.a.x + pos.a.width < pos.b.x);
assert(result.edges.every(e => e.source.y + e.source.height < e.target.y));
assert.deepEqual([...highlightedPath(folded).nodes], ['winner','gate','holdoutB','devB','b','gen','base','start']);
""")
