import shutil
import subprocess
from pathlib import Path

import pytest


def test_runtime_mount_waits_for_restart_and_reads_the_persisted_job():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    module = Path("src/sca/api/static/portal-runtime.js").resolve()
    harness = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; }
  setAttribute() {}
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
global.document = {createElement: tag => new Element(tag)};
let reloaded = 0;
global.window = {
  authHeaders: () => ({Authorization: 'Bearer fixture-eval'}),
  confirm: text => text.includes('切换时自动迁移历史记录'),
  location: {reload: () => reloaded++},
};
const target = new Element('div');
const status = () => target.children[4].textContent;
const delays = [];
global.setTimeout = (next, ms) => {
  delays.push(ms);
  if (delays.length === 2) assert.equal(status(), '正在重启…（导出历史记录）');
  next();
};
const endpoint = '/v1/portal/runtime';
const source = {current_mode: 'source', preferred_mode: null,
  modes: [{mode: 'source', available: true}, {mode: 'docker', available: true}], switch: null};
const exporting = {...source, preferred_mode: 'docker',
  switch: {id: 'test-job', status: 'running', step: 'export', log_tail: '导出历史记录'}};
const done = {...exporting, current_mode: 'docker', switch: {
  id: 'test-job', status: 'succeeded', step: 'done', log_tail: '切换完成', error: null,
}};
const requests = [
  [endpoint, source], [endpoint + '/switch', {id: 'test-job'}], ['/healthz', {}],
  [endpoint, exporting], ['/healthz', null], ['/healthz', {}], [endpoint, done],
];
global.fetch = async (url, options) => {
  const [path, data] = requests.shift();
  assert.equal(url, path);
  if (url !== '/healthz') assert.equal(options.headers.Authorization, 'Bearer fixture-eval');
  if (path.endsWith('/switch')) {
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {mode: 'docker', carry_history: true});
  }
  if (data === null) throw TypeError('connection reset');
  return {ok: true, json: async () => data};
};
require(process.argv[1]);
(async () => {
  await window.SCAPortalRuntime.mount(target);
  assert.equal(target.children[1].textContent, '当前运行模式 源码模式');
  const [native, docker] = target.children[2].children;
  assert.equal(native.children.at(-1).disabled, true);
  await docker.children.at(-1).onclick();
  assert.deepEqual(delays, [1000, 2000]);
  assert.equal(target.children[1].textContent, '当前运行模式 Docker 模式');
  assert.equal(status(), '运行模式切换完成。');
  assert.equal(reloaded, 1);
  assert.deepEqual(requests, []);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", harness, str(module)], timeout=10, check=True)
