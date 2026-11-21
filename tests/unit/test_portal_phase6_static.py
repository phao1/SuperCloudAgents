import re
import shutil
import subprocess
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

STATIC = Path("src/sca/api/static").resolve()
ASSETS = [
    "portal-ui.js", "portal-markdown.js", "portal-tasks.js",
    "portal-evolution.js", "portal-settings.js",
    "portal-projects.js", "task-dag.js",
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


def test_project_dialog_visibility_uses_the_native_open_state():
    page = (STATIC / "portal.html").read_text()
    dialog = re.search(r'<dialog\b[^>]*id="project-dialog"[^>]*>', page)
    css = (STATIC / "portal-operations.css").read_text()
    assert dialog and not re.search(r'\b(?:open|hidden)\b', dialog[0])
    assert '<form method="dialog"><button type="submit">关闭</button></form>' in page
    assert re.search(r'\.project-dialog:not\(\[open\]\)\s*\{\s*display:\s*none;', css)


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
const guest={tenant_id:'eval',role:'viewer',guest:true};
assert(portalUI.can('history',guest));
assert(portalUI.can('eval-history',guest));
assert(['tenant','member','admin','eval','eval-admin','platform'].every(role=>!portalUI.can(role,guest)));
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


def test_portal_guest_history_bootstrap(tmp_path):
    source = (STATIC / "portal.html").read_text()
    assert '只读访客模式 · <a href="#/overview">登录</a>' in source
    assert '未配置模型密钥：可浏览历史，无法运行新任务' in source
    assert 'id="task-message-form" class="task-message-form stack" data-access="member" hidden' in source
    assert "headers:authHeaders(),signal:stream.controller.signal" in source

    node = shutil.which("node") or Path.home() / ".local/bin/node"
    if not Path(node).exists():
        pytest.skip("Node is unavailable")
    script = source.split('<script>')[1].split('</script>')[0]
    sections = [
        ("function authHeaders()", "function options("),
        ("async function loadConfig(", "async function loadSuites("),
        ("const pages=", "async function route("),
        ("async function loadRecentTasks(", "$('refresh').onclick="),
    ]
    harness = r'''
const assert=require('node:assert/strict');
const elements=new Map(),location={hash:''},history={replaceState:(a,b,hash)=>{location.hash=hash}};
function $(id){
  if(!elements.has(id))elements.set(id,{hidden:true,textContent:'',querySelector:()=>$('save')});
  return elements.get(id);
}
const controls=['history','member','admin'].map(access=>({dataset:{access},hidden:true}));
const document={querySelectorAll:()=>controls};
const localStorage={removeItem:()=>{}};
let portalToken='',portalConfig=null,portalIdentity=null,guestChecked=false,refreshing=false;
let canRescore=false,savingKey=false,serverTasks=[],routeCount=0;
const calls=[];
const json=JSON.stringify;
function error(e){throw e}
function displayConnection(){}
function updateRescoreButton(){}
function options(){}
function renderProviderError(){}
function renderTaskChoices(){}
async function loadVersions(){}
async function route(){routeCount++}
function resetPortalSession(){portalIdentity=null}
global.fetch=async (path,request)=>{
  calls.push([path,request.headers.Authorization]);
  if(path==='/v1/portal/whoami')return {ok:true,json:async()=>({tenant_id:'customer',role:'member'})};
  if(path==='/v1/portal/config')return {ok:true,json:async()=>request.headers.Authorization
    ?{guest_view:false,model_configured:true,models:[],agent_backends:[]}
    :{guest_view:true,guest_tenant:'eval',model_configured:false}};
  return {ok:true,json:async()=>[]};
};
'''
    checks = r'''
(async()=>{
  await refresh();
  assert.deepEqual(calls,[['/v1/portal/config',undefined],['/v1/tasks?limit=100',undefined]]);
  assert.equal(pageRoute().current,'tasks');
  assert.equal(routeCount,1);
  assert.equal($('guest-view-banner').hidden,false);
  assert.equal($('model-not-configured').hidden,false);
  assert.deepEqual(controls.map(control=>control.hidden),[false,true,true]);
  await assert.rejects(api('/tasks',{}),/请先登录/);
  assert.equal(calls.length,2);

  location.hash='#/projects/fixture-project';
  assert.deepEqual(pageRoute(),{page:'tasks',parts:[],current:'tasks',redirect:'#/tasks'});
  portalToken='fixture-member';
  await refresh(true);
  assert.deepEqual(calls.slice(2),[
    ['/v1/portal/whoami','Bearer fixture-member'],['/v1/portal/config','Bearer fixture-member'],
    ['/v1/tasks?limit=100','Bearer fixture-member'],
  ]);
  assert.equal(pageRoute().current,'projects');
  assert.equal($('guest-view-banner').hidden,true);
  assert.equal($('model-not-configured').hidden,true);
  assert.deepEqual(controls.map(control=>control.hidden),[false,false,true]);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    snippets = "\n".join(script[script.index(start):script.index(end)] for start, end in sections)
    path = tmp_path / "guest.cjs"
    path.write_text(harness + (STATIC / "portal-ui.js").read_text() + snippets + checks)
    subprocess.run([str(node), str(path)], check=True, timeout=20)


@contextmanager
def portal_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as engine:
        if not Path(engine.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is unavailable")
        try:
            browser = engine.chromium.launch()
        except playwright.Error as exc:
            if "sandbox_host_linux.cc" in str(exc) and "Operation not permitted" in str(exc):
                pytest.skip("Chromium startup is denied by the sandbox")
            raise
        try:
            yield browser
        finally:
            browser.close()


def serve_portal(route, path):
    if path == "/portal":
        route.fulfill(status=200, content_type="text/html", body=(STATIC / "portal.html").read_text())
        return True
    name = path.rsplit("/", 1)[-1]
    if name in {*ASSETS, "portal-operations.css", "task-dag.css", "task-flow.js", "task-timeline.js"}:
        kind = "text/javascript" if name.endswith(".js") else "text/css"
        route.fulfill(status=200, content_type=kind, body=(STATIC / name).read_text())
        return True
    return False


def test_portal_phase6_browser_submission():
    calls = []
    task_id = "12abc345-1234-1234-1234-123456789012"

    def handle(route):
        req = route.request
        path = urlsplit(req.url).path
        if serve_portal(route, path):
            return
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
            f"/v1/tasks/{task_id}/dag": {"nodes": [], "edges": []},
        }
        return route.fulfill(status=200, json=fixtures.get(path, []))

    with portal_browser() as browser:
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


def test_projects_browser_shared_modal_lifecycle():
    project_id = "31a893e0-3687-4449-8d39-248747aa258b"
    endpoint = "/v1/projects/" + project_id
    project = {"id": project_id, "name": "默认项目", "status": "active", "group_counts": {}, "repos": []}
    document = {"id": "fixture-doc", "title": "项目手册", "text": "手册内容", "format": "text"}

    def handle(route):
        req = route.request
        path = urlsplit(req.url).path
        if serve_portal(route, path):
            return
        if path == endpoint and req.method == "PATCH":
            project.update(req.post_data_json)
        fixtures = {
            "/v1/portal/whoami": {"tenant_id": "acme", "role": "admin"},
            "/v1/portal/config": {"agent_backends": ["codex"], "default_agent_backend": "codex"},
            "/v1/projects": [project], endpoint: project,
            endpoint + "/library": {"documents": [document], "artifacts": []},
            "/v1/knowledge/documents/fixture-doc": document,
        }
        route.fulfill(status=200, json=fixtures.get(path, []))

    with portal_browser() as browser:
        page = browser.new_page()
        page.route("**/*", handle)
        page.add_init_script("localStorage.setItem('sca-portal-token', 'fixture-admin')")
        page.goto("http://portal.test/portal#/projects")
        card = page.get_by_role("link", name="默认项目", exact=True)
        card.wait_for()
        dialog = page.locator("#project-dialog")
        dialog.wait_for(state="hidden")
        assert dialog.evaluate("node => getComputedStyle(node).display") == "none"
        create = page.get_by_role("button", name="新建项目")
        create.click()
        dialog.get_by_label("项目名称").fill("未保存")
        dialog.get_by_role("button", name="关闭").click()
        dialog.wait_for(state="hidden")
        create.click()
        assert dialog.get_by_label("项目名称").input_value() == ""
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        create.click()
        dialog.get_by_role("heading", name="新建项目").click()
        assert dialog.is_visible()
        page.mouse.click(3, 3)
        dialog.wait_for(state="hidden")

        card.click()
        page.get_by_role("button", name="新线程").click()
        dialog.get_by_role("heading", name="新线程").wait_for()
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        page.locator("#project-menu summary").click()
        page.get_by_role("button", name="设置", exact=True).click()
        dialog.get_by_label("项目名称").fill("已更新项目")
        dialog.get_by_role("button", name="保存设置").click()
        dialog.get_by_text("项目设置已保存。").wait_for()
        page.mouse.click(3, 3)
        dialog.wait_for(state="hidden")
        page.get_by_role("heading", name="已更新项目").wait_for()

        page.get_by_role("link", name="文件库", exact=True).click()
        page.get_by_role("button", name="上传文档").click()
        dialog.get_by_role("heading", name="上传项目文档").wait_for()
        dialog.get_by_role("button", name="关闭").click()
        page.get_by_role("button", name="项目手册").click()
        dialog.get_by_text("手册内容").wait_for()
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")

        page.get_by_role("link", name="设置", exact=True).click()
        page.get_by_role("button", name="删除项目").click()
        dialog.get_by_role("button", name="确认删除").wait_for()
        page.mouse.click(3, 3)
        dialog.wait_for(state="hidden")
