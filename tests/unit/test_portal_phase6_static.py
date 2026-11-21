import json
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
TENANT_PORTAL_CONFIG = {
    "guest_view": False,
    "model_configured": True,
    "models": ["model-a"],
    "default_model": "model-a",
    "agent_backends": ["codex"],
    "default_agent_backend": "codex",
}
GUEST_PORTAL_CONFIG = {"guest_view": True, "guest_tenant": "eval", "model_configured": False}


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


@pytest.mark.parametrize("destination", ["guest", "projects", "task"])
def test_portal_route_bootstrap(tmp_path, destination):
    source = (STATIC / "portal.html").read_text()
    assert '只读访客模式 · <a href="#/overview">登录</a>' in source
    assert '未配置模型密钥：可浏览历史，无法运行新任务' in source
    assert 'id="task-message-form" class="task-message-form stack" data-access="member" hidden' in source
    assert "headers:authHeaders(),signal:stream.controller.signal" in source

    node = shutil.which("node") or Path.home() / ".local/bin/node"
    if not Path(node).exists():
        pytest.skip("Node is unavailable")
    script = source.split("<script>", 1)[1].split("window.addEventListener('hashchange'", 1)[0]
    harness = r'''
const assert=require('node:assert/strict');
const destination=process.argv[2];
const task={id:'task-id',kind:'coding',state:'SUCCEEDED',spec:{},result:{}};
const project={id:'project-id',name:'默认项目',status:'active',group_counts:{resolved:1030}};
const taskPath='/v1/tasks/'+task.id;
const location={hash:{guest:'#/projects/private-project',projects:'#/projects',task:'#/tasks/'+task.id}[destination]};
const history={replaceState:(_state,_title,hash)=>{location.hash=hash}};
const localStorage={getItem:key=>key==='sca-portal-token'&&destination!=='guest'?'fixture-member':null};
class Element {
  constructor(id) {
    Object.assign(this,{id,hidden:true,children:[],value:'',textContent:'',dataset:{},parentElement:{}});
    this.classList={toggle(){}};
    this.attributes=new Map();
  }
  get options(){return this.children}
  append(...nodes){this.children.push(...nodes)}
  replaceChildren(...nodes){this.children=nodes}
  addEventListener(){}
  hasAttribute(name){return this.attributes.has(name)}
  setAttribute(name,value){this.attributes.set(name,value)}
  getAttribute(name){return this.attributes.get(name)}
  querySelector(){return new Element()}
  querySelectorAll(){return []}
  reset(){}
}
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,new Element(id));
  return elements.get(id);
}
const sections=['overview','projects-page','evaluation','trace'].map(element);
const controls=['history','member','admin'].map(access=>({dataset:{access},hidden:true}));
const document={
  getElementById:element,createElement:()=>new Element(),documentElement:new Element(),
  addEventListener(){},
  querySelectorAll:selector=>({'main > section':sections,'[data-access]':controls})[selector]||[],
};
const window={addEventListener(){}};
const SCATaskTimeline={create:()=>({update:()=>null,selectedSeq:()=>null,includes:()=>true})};
const calls=[];
const blocked=new Map();
function block(path){
  let release;
  const pending=new Promise(resolve=>{release=resolve});
  blocked.set(path,pending);
  return ()=>{blocked.delete(path);release()};
}
const releaseSuites=block('/v1/evals/suites');
const releaseProjects=destination==='projects'?block('/v1/projects'):null;
const releaseTask=destination==='task'?block(taskPath):null;
const releaseConfig=destination==='task'?block('/v1/portal/config'):null;
const releaseRecent=destination==='task'?block('/v1/tasks?limit=100'):null;
global.fetch=async (path,request)=>{
  const auth=request.headers.Authorization;
  if(['/v1/portal/whoami','/v1/projects'].includes(path))assert.equal(auth,'Bearer fixture-member');
  calls.push(path);
  await blocked.get(path);
  const fixtures={
    '/v1/portal/whoami':{tenant_id:'eval',role:'member'},
    '/v1/portal/config':auth?config.tenant:config.guest,
    '/v1/projects':[project],
    '/v1/evals/suites':[{name:'opencode-demo',cases:[]}],
    [taskPath]:task,
    [taskPath+'/events?after=0']:[{seq:1,type:'task.created',payload:{}}],
  };
  return {ok:true,json:async()=>fixtures[path]||[]};
};
'''
    checks = r'''
const deadline=setTimeout(()=>{throw Error('Portal bootstrap did not settle')},4000);
(async()=>{
  const nextTick=()=>new Promise(setImmediate);
  await route();
  const startup=refresh();
  await nextTick();
  if(destination==='guest'){
    await startup;
    assert.deepEqual(calls,['/v1/portal/config','/v1/tasks?limit=100']);
    assert.equal(location.hash,'#/tasks');
    assert.equal($('trace').hidden,false);
    assert.equal($('guest-view-banner').hidden,false);
    assert.equal($('model-not-configured').hidden,false);
    assert.deepEqual(controls.map(control=>control.hidden),[false,true,true]);
    await assert.rejects(api('/tasks',{}),/请先登录/);
    assert.equal(calls.length,2);

    portalToken='fixture-member';
    location.hash='#/projects';
    await refresh(true);
    assert.deepEqual(calls.slice(2),['/v1/portal/whoami','/v1/projects']);
    assert.equal($('projects-home').hidden,false);
    assert.equal($('guest-view-banner').hidden,true);
    assert.equal($('model-not-configured').hidden,true);
    assert.deepEqual(controls.map(control=>control.hidden),[false,false,true]);
  }else if(destination==='projects'){
    assert.deepEqual(calls,['/v1/portal/whoami','/v1/projects']);
    const duplicate=route();
    await refresh();
    assert.equal(calls.filter(path=>path==='/v1/projects').length,1);
    releaseProjects();
    await Promise.all([startup,duplicate]);
    assert.equal($('projects-home').hidden,false);
    const [card]=$('project-cards').children;
    assert.equal(card.children[0].children[0].children[0].textContent,'默认项目');
    assert.equal(card.children[1].children.at(-1).children[1].textContent,'1,030');
    await refresh();
    await refresh();
    assert.deepEqual(calls,['/v1/portal/whoami','/v1/projects']);

    location.hash='#/evals/repair';
    const evaluation=route();
    await nextTick();
    assert(calls.includes('/v1/evals/suites'));
    location.hash='#/projects';
    await route();
    assert.equal($('projects-home').hidden,false);
    assert.equal(calls.filter(path=>path==='/v1/projects').length,2);
    releaseSuites();
    await evaluation;
    assert(!calls.includes('/v1/evals/runs?limit=200'));
    await refresh(true);
    assert.equal(calls.filter(path=>path==='/v1/projects').length,3);
  }else{
    assert.deepEqual(new Set(calls),new Set([
      '/v1/portal/whoami',taskPath,taskPath+'/events?after=0',
      '/v1/tasks?limit=100','/v1/portal/config',
    ]));
    releaseTask();
    await nextTick();
    assert.equal($('task-details').hidden,false);
    assert.equal($('task-title').textContent,task.id);
    assert.equal($('task-state').textContent,'已成功');
    assert.equal($('task-picker').disabled,undefined);
    releaseConfig();
    releaseRecent();
    await startup;
    assert.equal($('task-picker').disabled,true);
    assert.equal(eventCursor,1);
  }
  assert.equal($('error').textContent,'');
})().finally(()=>clearTimeout(deadline)).catch(error=>{console.error(error);process.exitCode=1});
'''
    config = json.dumps({"tenant": TENANT_PORTAL_CONFIG, "guest": GUEST_PORTAL_CONFIG})
    assets = ["task-flow.js", *(name for name in ASSETS if name != "task-dag.js")]
    path = tmp_path / "portal.cjs"
    path.write_text("\n".join([
        f"const config = {config};", harness, *((STATIC / name).read_text() for name in assets),
        script, checks,
    ]))
    subprocess.run([str(node), str(path), destination], check=True, timeout=10)


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


@pytest.mark.parametrize("destination", ["projects", "task"])
def test_portal_browser_first_render(destination):
    task_id = "12abc345-1234-1234-1234-123456789012"
    task_path = f"/v1/tasks/{task_id}"
    project = {
        "id": "31a893e0-3687-4449-8d39-248747aa258b", "name": "默认项目",
        "status": "active", "group_counts": {"resolved": 1030},
    }
    requests = []

    def handle(route):
        url = urlsplit(route.request.url)
        path = url.path
        if serve_portal(route, path):
            return
        if path.startswith("/v1/"):
            requests.append(path)
        if path in {"/v1/evals/suites", "/v1/agents/opencode/versions", "/v1/portal/config", "/v1/tasks"}:
            return
        fixtures = {
            "/v1/portal/whoami": {"tenant_id": "eval", "role": "member"},
            "/v1/projects": [project],
            task_path: {"id": task_id, "kind": "coding", "state": "SUCCEEDED", "spec": {}, "result": {}},
            task_path + "/dag": {"nodes": [], "edges": []},
        }
        route.fulfill(json=fixtures.get(path, []))

    with portal_browser() as browser:
        page = browser.new_page()
        page.route("**/*", handle)
        page.add_init_script("localStorage.setItem('sca-portal-token', 'fixture-member')")
        route = "projects" if destination == "projects" else f"tasks/{task_id}"
        page.goto("http://portal.test/portal#/" + route, wait_until="domcontentloaded")
        if destination == "projects":
            card = page.locator(".project-card")
            card.get_by_role("link", name="默认项目", exact=True).wait_for(timeout=3000)
            assert card.get_by_text("1,030", exact=True).is_visible()
            page.wait_for_function("!refreshing")
            page.evaluate("refresh()")
            assert requests == ["/v1/portal/whoami", "/v1/projects"]
        else:
            page.get_by_role("heading", name=task_id, exact=True).wait_for(timeout=3000)
            assert task_path + "/events" in requests
            assert "/v1/evals/suites" not in requests
            assert "/v1/agents/opencode/versions" not in requests


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
        nav = page.locator('nav[aria-label="主导航"]')
        assert not nav.locator('a[href="#/team"]').is_visible()
        assert not nav.locator('a[href="#/platform"]').is_visible()
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
        if path == "/v1/tasks":
            return route.fulfill(json=[])
        authenticated = req.header_value("authorization") == "Bearer fixture-admin"
        if path == "/v1/portal/config":
            config = TENANT_PORTAL_CONFIG if authenticated else GUEST_PORTAL_CONFIG
            return route.fulfill(json=config)
        if not authenticated:
            return route.fulfill(status=401)
        if path == endpoint and req.method == "PATCH":
            project.update(req.post_data_json)
        fixtures = {
            "/v1/portal/whoami": {
                "tenant_id": "acme", "subject": "fixture-admin", "role": "admin",
                "platform_admin": False,
            },
            "/v1/agents/opencode/versions": [],
            "/v1/tools/available": [],
            "/v1/projects": [project],
            endpoint: project,
            endpoint + "/threads": [],
            endpoint + "/library": {"documents": [document], "artifacts": []},
            "/v1/knowledge/documents/fixture-doc": document,
        }
        if path in fixtures:
            return route.fulfill(json=fixtures[path])
        route.fulfill(status=404)

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
        page.close()

        guest = browser.new_page()
        guest.route("**/*", handle)
        guest.goto("http://portal.test/portal#/projects")
        guest.wait_for_url("**/portal#/tasks")
        guest.locator("#trace").wait_for()
        assert guest.locator("#guest-view-banner").is_visible()
        nav = guest.locator('nav[aria-label="主导航"]')
        assert not nav.locator('a[href="#/projects"]').is_visible()
