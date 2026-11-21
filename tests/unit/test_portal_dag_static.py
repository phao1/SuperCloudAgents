import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path("src/sca/api/static")


def test_task_dag_panel_uses_local_assets_and_the_existing_stream():
    page = (STATIC / "portal.html").read_text()
    panel = page[page.index('id="task-dag"'):page.index('id="swimlane"')]
    assert "执行图 DAG" in panel
    assert 'aria-label="节点详情"' in panel
    assert 'class="dag-fit"' in panel
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
    assert "max-width: 550px" in css
    for status in ("pending", "running", "succeeded", "failed", "cancelled", "skipped"):
        assert f"{status}:" in script
    for kind in ("sequence", "dependency", "spawn", "select", "merge"):
        assert f".dag-edge-{kind}" in css


def test_dag_layout_layers_branches_and_keeps_edges_forward():
    node = shutil.which("node") or str(Path.home() / ".local/bin/node")
    if not Path(node).exists():
        pytest.skip("Node is unavailable")
    script = """
const assert = require('node:assert/strict');
const {layout} = require(process.argv[1]);
const dag = {
  nodes: ['end', 'b', 'root', 'a', 'join'].map(id => ({id})),
  edges: [
    {from: 'root', to: 'a', kind: 'spawn'}, {from: 'root', to: 'b', kind: 'spawn'},
    {from: 'a', to: 'join', kind: 'dependency'}, {from: 'b', to: 'join', kind: 'dependency'},
    {from: 'join', to: 'end', kind: 'merge'},
  ],
};
const result = layout(dag);
const positions = Object.fromEntries(result.nodes.map(n => [n.node.id, n]));
assert.equal(result.edges.length, dag.edges.length);
assert.equal(positions.a.x, positions.b.x);
assert.notEqual(positions.a.y, positions.b.y);
assert(result.edges.every(e => e.source.x < e.target.x));
assert(positions.end.x > positions.join.x);
"""
    subprocess.run([node, "-e", script, str((STATIC / "task-dag.js").resolve())],
                   check=True, timeout=10)
