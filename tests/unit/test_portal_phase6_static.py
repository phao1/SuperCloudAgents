import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

STATIC = Path("src/sca/api/static").resolve()
ASSETS = [
    "portal-ui.js", "portal-markdown.js", "portal-tasks.js",
    "portal-evolution.js", "portal-settings.js",
]


class PortalAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
        self.nav = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("src", "href"):
            if key in attrs:
                self.refs.append(attrs[key])
        if tag == "a" and attrs.get("href", "").startswith("#/") and "data-access" in attrs:
            self.nav[attrs["href"][2:]] = attrs["data-access"], "hidden" in attrs


def test_phase6_portal_navigation_and_local_assets():
    parser = PortalAssets()
    source = (STATIC / "portal.html").read_text()
    parser.feed(source)
    parser.close()
    for route, title, role in (
        ("submit", "提交任务", "member"), ("evolutions", "自动演进", "admin"),
        ("tools", "工具与知识库", "tenant"), ("integrations", "集成", "admin"),
        ("team", "团队与用量", "admin"), ("platform", "平台管理", "platform"),
    ):
        assert parser.nav[route] == (role, True)
        assert f"""'{route}-page','{title}'""" in source
    for name in (*ASSETS, "portal-operations.css"):
        assert "/portal/assets/" + name in parser.refs
    assert all(not urlsplit(ref).scheme and not urlsplit(ref).netloc for ref in parser.refs)
    assert "'/portal/whoami'" in source
    for label in ("代码影响分析", "范围越界", "实时终端（只读）", "Best-of-N 尝试", "拉取请求与工单"):
        assert label in source
    for target in ("analysis-rendered", "analysis-rendered-detail"):
        assert f'id="{target}" class="markdown"' in source
    for file in ("portal.html", *ASSETS):
        text = (STATIC / file).read_text()
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", text)


@pytest.mark.parametrize(
    ("file", "paths"),
    [
        ("portal-ui.js", ("/v1/files",)),
        ("portal-tasks.js", ("/tasks", "/tools/available", "/best-of", "/select",
                             "/impact", "/terminal", "/artifacts/report.md", "/pr")),
        ("portal-evolution.js", ("/evolutions", "/evolution-schedules", "/champions",
                                 "/champions/rollouts", "/evals/harvest", "/run-now",
                                 "/approve", "/reject", "/stop", "/rollback", "/solidify")),
        ("portal-settings.js", ("/tools", "/grants", "/knowledge/documents",
                                "/knowledge/search", "/integrations/scm", "/v1/webhooks/",
                                "/tenant/members", "/tenant/tokens", "/portal/tenant-quota",
                                "/usage", "/usage/export.csv", "/admin/tenants", "/admin/plans",
                                "/admin/usage", "/suspend", "/resume")),
    ],
)
def test_phase6_api_paths(file, paths):
    source = (STATIC / file).read_text()
    for path in paths:
        assert f"'{path}'" in source


def test_markdown_overlay_diff_and_roles_runtime(tmp_path):
    node = shutil.which("node") or Path.home() / ".local/bin/node"
    if not Path(node).exists():
        pytest.skip("Node is unavailable")
    harness = r'''
const assert = require('node:assert/strict');
const location = {href:'https://sca.example/portal'};
const document = {
  createTextNode:value=>({tag:'#text', textContent:value}),
  createDocumentFragment:()=>el('#fragment'),
};
function el(tag,textContent=''){
  return {tag,textContent,children:[],append(...nodes){this.children.push(...nodes)},
    setAttribute(name,value){this[name]=value}};
}
function evidenceLink(label,href){return {...el('a',label),href};}
const overallPassed = row => row.acceptance_passed === true && row.trajectory_passed !== false;
const money = value => '$' + Number(value).toFixed(4);
function walk(node){return [node,...(node.children||[]).flatMap(walk)];}
'''
    checks = r'''
const markdown = [
  '# <script>alert(1)</script>', '', '1. **加粗** 和 *斜体*', '2. \x60<svg>\x60', '',
  '- 项目', '', '| 指标 | 值 |', '| --- | ---: |', '| a \\| b | 1 |', '',
  '\x60\x60\x60html', '<iframe srcdoc="<script>bad</script>">', '\x60\x60\x60', '',
  '<img src=x onerror=alert(1)> &lt;script&gt;', '',
  '[合法](https://example.test/docs) [相对](/docs) [非法](javascript:alert)',
  '[非法](java\tscript:alert) [非法](data:text/html,evil) [非法](//other.test/a)',
  '![图片](https://example.test/illustration.png)',
].join('\n');
const nodes = walk(portalMarkdown.render(markdown));
for (const tag of ['h1','ol','ul','li','strong','em','table','th','td','pre','code']){
  assert(nodes.some(node=>node.tag===tag), tag);
}
assert(!nodes.some(node=>['img','script','iframe','svg'].includes(node.tag)));
assert(nodes.some(node=>node.tag==='#text'&&node.textContent.includes('<script>alert(1)</script>')));
assert(nodes.some(node=>node.tag==='code'&&node.textContent.includes('<iframe')));
assert(nodes.some(node=>node.textContent.includes('a | b')));
const links = nodes.filter(node=>node.tag==='a');
assert.deepEqual(links.map(node=>node.href),['https://example.test/docs','https://sca.example/docs']);
assert(links.every(node=>node.rel==='noopener noreferrer'));
for (const role of ['viewer','member','admin']){
  const identity={tenant_id:'acme',role};
  assert(portalUI.can('tenant',identity));
  assert.equal(portalUI.can('member',identity),role!=='viewer');
  assert.equal(portalUI.can('admin',identity),role==='admin');
  assert(!portalUI.can('platform',identity));
  assert(!portalUI.can('eval-admin',identity));
}
assert(portalUI.can('eval-admin',{tenant_id:'eval',role:'admin'}));
assert(portalUI.can('platform',{platform_admin:true}));
assert(!portalUI.can('tenant',{platform_admin:true}));
assert.deepEqual(portalUI.backendChoices({agent_backends:['codex','dsh'],default_agent_backend:'dsh'}),
  [['','默认 · DeepSeek Harness'],['codex','Codex'],['dsh','DeepSeek Harness']]);
assert.deepEqual(portalEvolution.lineChanges('相同\n旧行\n下文','相同\n新行\n下文'),
  [' 相同','-旧行','+新行',' 下文']);
assert.deepEqual(portalEvolution.lineChanges(undefined,'新增'),['+新增']);
const baseline={pass_rate:.4,cost_usd:3};
const winner={generation:1,decision:'promoted',dev_metrics:{pass_rate:.8,cost_usd:2},dev_run_id:'winner'};
const groups=portalEvolution.generations([winner,{generation:2}],baseline,'base');
assert.deepEqual(groups.map(group=>group.champion),[baseline,winner.dev_metrics]);
assert.deepEqual(groups.map(group=>group.run),['base','winner']);
const a={suite_hash:'fixtures',cost_usd:1,passed:1,results:[
  {case_id:'one',repeat:0,acceptance_passed:true},{case_id:'one',repeat:1,acceptance_passed:false},
]};
const b={...a,results:[{...a.results[0],trajectory_passed:false},{...a.results[1],acceptance_passed:true}]};
assert.match(portalEvolution.equivalence(a,b),/^0 \/ 2 /);
assert.match(portalEvolution.equivalence(a,{...a,results:a.results.slice(1)}),/无法完整配对/);
'''
    scripts = ["portal-ui.js", "portal-markdown.js", "portal-evolution.js"]
    path = tmp_path / "portal-phase6.cjs"
    path.write_text(harness + "\n".join((STATIC / file).read_text() for file in scripts) + checks)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


def test_portal_phase6_browser_submission():
    playwright = pytest.importorskip("playwright.sync_api")
    calls = []
    task_id = "12abc345-1234-1234-1234-123456789012"

    def handle(route):
        req = route.request
        path = urlsplit(req.url).path
        if path == "/portal":
            return route.fulfill(status=200, content_type="text/html", body=(STATIC / "portal.html").read_text())
        if path.rsplit("/", 1)[-1] in {*ASSETS, "portal-operations.css", "task-flow.js", "task-timeline.js"}:
            name = path.rsplit("/", 1)[-1]
            kind = "text/javascript" if name.endswith(".js") else "text/css"
            return route.fulfill(status=200, content_type=kind, body=(STATIC / name).read_text())
        if path == "/v1/tasks" and req.method == "POST":
            calls.append(req.post_data_json)
            if len(calls) == 1:
                return route.fulfill(status=422, json={"error": {"message": "请检查仓库地址"}})
            return route.fulfill(status=201, json={"id": task_id})
        fixtures = {
            "/v1/portal/whoami": {"tenant_id": "acme", "role": "member", "platform_admin": False},
            "/v1/portal/config": {"models": ["model-a"], "agent_backends": ["opencode", "codex", "dsh"],
                                  "default_agent_backend": "codex", "default_model": "model-a"},
            "/v1/agents/opencode/versions": [],
            "/v1/tools/available": [{"name": "knowledge", "description": "知识检索"}],
            "/v1/tasks": [],
            f"/v1/tasks/{task_id}": {
                "id": task_id, "state": "SUCCEEDED", "kind": "coding",
                "spec": {"instruction": "补测试"}, "result": {},
            },
        }
        return route.fulfill(status=200, json=fixtures.get(path, []))

    with playwright.sync_playwright() as engine:
        if not Path(engine.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is unavailable")
        browser = engine.chromium.launch()
        page = browser.new_page()
        page.route("**/*", handle)
        page.goto("http://portal.test/portal#/submit")
        page.locator("#token").fill("fixture-member")
        page.get_by_role("button", name="连接", exact=True).click()
        page.get_by_role("heading", name="提交任务").wait_for()
        assert not page.locator('nav a[href="#/team"]').is_visible()
        assert not page.locator('nav a[href="#/platform"]').is_visible()
        page.get_by_label("仓库地址").fill("https://example.test/org/repo.git")
        page.get_by_label("任务指令").fill("补测试")
        page.get_by_label("Agent 后端").select_option("dsh")
        assert page.get_by_label("OpenCode 版本（可选）").is_disabled()
        page.get_by_label("Agent 后端").select_option("opencode")
        form = page.locator("#submit-form")
        form.get_by_text("Best-of-N 与执行设置", exact=True).click()
        page.get_by_label("并行尝试数").select_option("2")
        page.get_by_label("选择方式").select_option("human")
        form.get_by_text("工具", exact=True).click()
        form.get_by_label("knowledge", exact=False).check()
        page.get_by_role("button", name="提交任务", exact=True).click()
        page.get_by_text("请检查仓库地址").wait_for()
        page.get_by_role("button", name="提交任务", exact=True).click()
        page.wait_for_url("**/portal#/tasks/" + task_id)
        page.get_by_role("heading", name="代码影响分析").wait_for()
        assert calls[1]["spec"]["best_of"] == {"n": 2, "select": "human"}
        assert calls[1]["spec"]["tools"] == ["knowledge"]
        assert calls[1]["spec"]["base_ref"] == "main"
        browser.close()
