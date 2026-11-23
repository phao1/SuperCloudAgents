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
const windows = {...source, platform: 'Windows', modes: [source.modes[0], {
  mode: 'docker', available: false, reason: 'Windows 版仅支持源码模式',
}]};
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
function checkCards(current, buttonText, disabled) {
  const [source, docker] = target.children[2].children;
  const [selected, other] = current === 'source' ? [source, docker] : [docker, source];
  assert.equal(selected.className, 'feature runtime-current');
  assert.equal(selected.children[0].children[1].textContent, '当前');
  assert.equal(selected.children.at(-1).textContent, '当前模式');
  assert.equal(selected.children.at(-1).disabled, true);
  assert.equal(other.className, 'feature');
  assert.equal(other.children[0].children.length, 1);
  assert.equal(other.children.at(-1).textContent, buttonText);
  assert.equal(other.children.at(-1).disabled, disabled);
  return other;
}
(async () => {
  await window.SCAPortalRuntime.mount(target);
  assert.equal(target.children[1].textContent, '当前运行模式 源码模式');
  const docker = checkCards('source', '切换到 Docker 模式', false);
  await docker.children.at(-1).onclick();
  assert.deepEqual(delays, [1000, 2000]);
  assert.equal(target.children[1].textContent, '当前运行模式 Docker 模式');
  assert.equal(status(), '运行模式切换完成。');
  assert.equal(reloaded, 1);
  window.SCAPortalRuntime.unmount(target);
  assert.equal(target.children.length, 0);
  requests.push([endpoint, done]);
  await window.SCAPortalRuntime.mount(target);
  assert.equal(target.children[1].textContent, '当前运行模式 Docker 模式');
  checkCards('docker', '切换到 源码模式', false);
  requests.push([endpoint, windows]);
  await window.SCAPortalRuntime.mount(target);
  const unavailable = checkCards('source', '切换到 Docker 模式', true);
  assert.equal(unavailable.children[1].children[1].textContent, 'Windows 版仅支持源码模式');
  assert.deepEqual(requests, []);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", harness, str(module)], timeout=10, check=True)
