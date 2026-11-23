import json
import re
import runpy
import shutil
import subprocess
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

STATIC = Path("src/sca/api/static").resolve()
ASSETS = [
    "portal-ui.js", "portal-conversation.js", "portal-models.js", "portal-markdown.js", "portal-tasks.js",
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
        ("submit", "提交任务", "member"), ("evolutions", "自动演进", "evolution-history"),
        ("tools", "工具与知识库", "tenant"), ("integrations", "集成", "admin"),
        ("team", "团队与用量", "admin"), ("platform", "平台管理", "platform"),
    ):
        assert parser.nav[route] == (role, True)
        assert f"""'{route}-page','{title}'""" in source
    for name in (*ASSETS, "portal-operations.css", "portal-conversation.css"):
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
        ("portal-conversation.js", ("/messages",)),
        ("portal-evolution.js", ("/evolutions", "/evolution-schedules", "/champions", "/models",
                                 "/champions/rollouts", "/evals/harvest", "/run-now",
                                 "/approve", "/reject", "/rollback", "/solidify")),
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
assert(portalUI.can('evolution-history',guest));
assert(!portalUI.can('evolution-history',{...guest,tenant_id:'acme'}));
assert(portalUI.can('evolution-history',{tenant_id:'acme',role:'admin'}));
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


@pytest.mark.parametrize("destination", [
    "guest", "local", "projects", "task", "evolution", "approvals", "result-approval", "evolution-approval",
    "local-result-approval", "local-evolution-approval", "archive",
])
def test_portal_route_bootstrap(tmp_path, destination):
    responses = {}
    if destination == "archive":
        responses = runpy.run_path("scripts/serve_portal_history.py")["archive_responses"]()
        source = responses["/portal"][1].decode("utf-8")
        parser = PortalAssets()
        parser.feed(source)
        refs = [ref for ref in parser.refs if ref.endswith((".js", ".css"))]
        assert all(responses[ref][1] for ref in refs)
        scripts = [responses[ref][1].decode("utf-8") for ref in refs if ref.endswith(".js")]
    else:
        source = (STATIC / "portal.html").read_text(encoding="utf-8")
        assets = ["task-flow.js", "task-timeline.js", *(name for name in ASSETS if name != "task-dag.js")]
        scripts = [
            "const SCATaskDAG=fixtureDAG;",
            *((STATIC / name).read_text(encoding="utf-8") for name in assets),
        ]
    assert '只读访客模式 · <a href="#/overview">登录</a>' in source
    assert '尚未接入模型，可浏览历史记录' in source
    assert 'id="task-conversation" aria-label="任务对话"' in source
    assert 'href="#/approvals" data-access="tenant" hidden' in source
    assert "headers:authHeaders(),signal:stream.controller.signal" in source

    node = shutil.which("node") or Path.home() / ".local/bin/node"
    if not Path(node).exists():
        pytest.skip("Node is unavailable")
    script = source.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const assert=require('node:assert/strict');
const destination=process.argv[2];
const archived=destination==='archive';
const guest=destination==='guest'||destination==='evolution';
const local=destination==='local'||destination.startsWith('local-');
const resultApproval=destination.endsWith('result-approval');
const evolutionApproval=destination.endsWith('evolution-approval');
const taskApproval=destination==='approvals'||resultApproval;
const awaitingReview=taskApproval||destination==='evolution';
const task={id:'task-id',kind:'coding',state:awaitingReview?'AWAITING_REVIEW':'SUCCEEDED',spec:{},result:
  resultApproval?{verification_passed:true,memory_proposals:['memory']} : {}};
const at='2026-11-22T00:00:00Z';
const approvalHref='#/tasks/'+task.id+'/approval';
const reviewRequests=[];
let moreMessages=false;
const harvest={id:'harvest-id',task_id:task.id,tenant_id:'eval',case_id:'boundary-case',suite:'fixture',status:'pending'};
const harvestHref='#/evolutions/harvest/approval/'+harvest.id;
const harvestPath='/v1/evals/harvest';
const missingReason='文件缺失（源机器上已清理）';
const artifacts=destination==='task'?['changes.patch','verify.log','report.md']
  .map(name=>({name,missing_reason:missingReason})):resultApproval?[{name:'changes.patch'}]:[];
const project={id:'project-id',name:'默认项目',status:'active',group_counts:{resolved:1030}};
const candidate={id:'candidate',generation:1,decision:'pending',reason:'awaiting approval',overlay:{},hypothesis:'Edge cases'};
const evolution={id:'evo-id',status:'awaiting_approval',spent_usd:0,
  config:{suite:'fixture',agent_version:'fake',model:'fake',budget_usd:1},candidates:[candidate],journal:[]};
let liveSpend=.0042;
const champion={id:'champion-id',candidate_id:candidate.id,active:true,parent_champion_id:'previous',
  agent_version:'fake',model:'fake',solidify_builds:[{version:'built',status:'succeeded',regression_status:'completed'}]};
const taskPath='/v1/tasks/'+task.id;
const evolutionPath='/v1/evolutions/'+evolution.id;
const candidateHref='#/evolutions/runs/'+evolution.id+'/approval/'+candidate.id;
const location={hash:taskApproval||evolutionApproval?'#/approvals':{
  guest:'#/projects/private-project',local:'#/projects',projects:'#/projects',task:'#/tasks/'+task.id,
  evolution:'#/evolutions',archive:''}[destination]};
const history={replaceState:(_state,_title,hash)=>{location.hash=hash}};
const token=evolutionApproval?'fixture-admin':'fixture-member';
const storage=new Map(!guest&&!local?[['sca-portal-token',token]]:[]);
const localStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
const evalAdmin={tenant_id:'eval',role:'admin',subject:'static-token'};
let required=false, acceptWarning=false, warningText='', switchWrites=0;
class Element {
  constructor(id) {
    Object.assign(this,{id,hidden:true,children:[],value:'',textContent:'',dataset:{},parentElement:{}});
    this.classList={add(){},toggle(){}};
    this.attributes=new Map();
    this.selectors=new Map();
  }
  get hash(){return this.href}
  get options(){return this.children}
  append(...nodes){this.children.push(...nodes)}
  prepend(...nodes){this.children.unshift(...nodes)}
  replaceChildren(...nodes){this.children=nodes}
  addEventListener(){}
  focus(){this.focused=true}
  scrollIntoView(){this.scrolled=true}
  hasChildNodes(){return this.children.length>0}
  hasAttribute(name){return this.attributes.has(name)}
  setAttribute(name,value){this.attributes.set(name,value)}
  getAttribute(name){return this.attributes.get(name)}
  removeAttribute(name){this.attributes.delete(name)}
  hasChildNodes(){return this.children.length>0}
  querySelector(selector){
    if(!this.selectors.has(selector))this.selectors.set(selector,new Element());
    return this.selectors.get(selector);
  }
  querySelectorAll(selector){return selector==='input'?walk(this).slice(1).filter(node=>node.type==='checkbox'):[]}
  reset(){}
}
const Node=Element;
const walk=node=>[node,...(node.children||[]).flatMap(walk)];
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,new Element(id));
  return elements.get(id);
}
const sections=['overview','projects-page','evaluation','trace','evolutions-page','memories-page','approvals-page'].map(element);
element('trace').after=node=>sections.push(node);
element('task-run-workbench').hidden=false;
element('evolution-dag-template').content={firstElementChild:{cloneNode:()=>element('evolution-dag')}};
const controls=['history','member','admin'].map(access=>({dataset:{access},hidden:true}));
const links=['tasks','evals','memories','evolutions','approvals'].map(page=>Object.assign(new Element(),{href:'#/'+page}));
const approvalLink=links.at(-1);
approvalLink.dataset.access='tenant';
links[0].after=link=>links.splice(1,0,link);
const nav=new Element();
nav.selectors.set('[href="#/tasks"]',links[0]);
nav.querySelectorAll=()=>links;
const document={
  getElementById:element,createElement:()=>new Element(),documentElement:new Element(),
  createElementNS:()=>new Element(),createTextNode:textContent=>({textContent}),
  createDocumentFragment:()=>new Element(),
  addEventListener(){},
  querySelector:selector=>({'.sidebar .brand':element('brand'),'.sidebar nav':nav})[selector],
  querySelectorAll:selector=>({'main > section':sections,'[data-access]':[...controls,approvalLink],'.sidebar nav a':links})[selector]||[],
};
const window=globalThis;
window.confirm=text=>{warningText=text;return acceptWarning};
const listeners=new Map();
window.addEventListener=(name,listener)=>listeners.set(name,listener);
const setInterval=()=>assert.equal(archived,false);
let graphsDestroyed=0;
const fixtureDAG=['task','evolution'].includes(destination)||evolutionApproval?{
  projectEvolution:()=>null,
  create(panel, fetchGraph, options){
    if(panel.id==='evolution-dag')assert.equal(options.project,SCATaskDAG.projectEvolution);
    else assert.equal(panel.id,'task-dag');
    return {refresh:fetchGraph,reset(){},destroy:()=>graphsDestroyed++};
  },
}:undefined;
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
let holdInbox;
global.fetch=async (path,request={})=>{
  const auth=request.headers?.Authorization;
  calls.push(path);
  if(archived){
    assert.equal(auth,undefined);
    const pathname=path.split('?')[0];
    assert(Object.hasOwn(config.archive,pathname),path);
    return {ok:true,json:async()=>config.archive[pathname]};
  }
  if(local&&path==='/v1/portal/access'){
    if(request.method==='PUT'){
      assert.equal(auth,required?'Bearer fixture-admin':undefined);
      required=JSON.parse(request.body).require_token;
      switchWrites++;
    }
    return {ok:true,json:async()=>({require_token:required,local_no_token_active:!required})};
  }
  const tenantAuth=local?(required?'Bearer fixture-admin':undefined):'Bearer '+token;
  if(['/v1/portal/whoami','/v1/projects','/v1/approvals',taskPath+'/review',evolutionPath+'/approve'].includes(path)
    ||path.startsWith(harvestPath)){
    assert.equal(auth,tenantAuth);
  }
  if(path.includes('/conversation?'))assert.equal(auth,guest?undefined:tenantAuth);
  if(path===taskPath+'/review'){
    reviewRequests.push(JSON.parse(request.body));
    task.state='RUNNING';
  }
  if(path===taskPath+'/artifacts/changes.patch'){
    return {ok:true,text:async()=> 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n'};
  }
  if(path===evolutionPath+'/approve'){
    reviewRequests.push(JSON.parse(request.body));
    candidate.decision='promoted';
    evolution.status='completed';
  }
  if(path===harvestPath+'/'+harvest.id+'/approve'){
    reviewRequests.push(JSON.parse(request.body));
    harvest.status='approved';
  }
  await blocked.get(path);
  const pending=evolutionApproval?[
    ...(evolution.status==='awaiting_approval'?[{id:candidate.id,href:candidateHref,kind:'evolution_candidate',
      title:candidate.hypothesis,created_at:at}]:[]),
    ...(harvest.status==='pending'?[{id:harvest.id,href:harvestHref,kind:'harvest',title:harvest.case_id,created_at:at}]:[]),
  ]:taskApproval&&task.state==='AWAITING_REVIEW'?[{href:approvalHref,id:task.id,
    kind:resultApproval?'task_result':'task_plan',title:'校验任务',created_at:at}]:[];
  const fixtures={
    '/v1/portal/whoami':local||evolutionApproval?evalAdmin:{tenant_id:'eval',role:'member'},
    '/v1/portal/config':local?(
      auth?{...config.tenant,portal_require_token:required}:
      required?config.guest:{...config.tenant,portal_require_token:false,local_identity:evalAdmin}
    ):auth?config.tenant:config.guest,
    '/v1/projects':[project],
    '/v1/approvals':{count:pending.length,items:pending},
    '/v1/evals/suites':[{name:'opencode-demo',cases:[]}],
    '/v1/memories?status=proposed':[{id:'memory',text:'检查空输入',scope:'repo',category:'convention'}],
    [taskPath]:task,
    [taskPath+'/events?after=0']:[{seq:1,type:'task.created',payload:{}},...(!awaitingReview?[]:[
      {seq:2,type:'review.requested',payload:resultApproval?{verification_passed:true}
        :{plan:{subtasks:[{name:'verify',instruction:'覆盖边界条件'}]}}},
    ])],
    [taskPath+'/artifacts']:artifacts,
    [taskPath+'/conversation?limit=100']:taskApproval?{items:[
      {id:'run',kind:'operations',backend:'dsh',activity:{turn_seq:1,
        operations:[{kind:'command',name:'pwd',exit_code:0,output_tail:'C:/w'}],files:[]},created_at:at},
    ],cursor:'2',has_more:true}:{items:[],cursor:'0',has_more:false},
    [taskPath+'/conversation?limit=100&cursor=2']:{items:[
      {id:'edit',kind:'operations',backend:'dsh',activity:{turn_seq:1,operations:[],files:[{path:'verify.py',additions:1}]},
        created_at:at},
      {id:'reply',kind:'reply',backend:'dsh',text:'**Done**',created_at:at},
      {id:'message',kind:'message',mode:'queue',message_id:'message',text:'看看边界',created_at:at},
      {id:'receipt',kind:'receipt',message_id:'message',status:'delivered',text:'已送达',created_at:at}
    ],cursor:'6',has_more:false},
    [taskPath+'/conversation?limit=100&cursor=6']:{items:moreMessages?[
      {id:'verification',kind:'verification.completed',text:'验证通过',created_at:at},
    ]:[],cursor:moreMessages?'7':'6',has_more:false},
    [taskPath+'/conversation?limit=100&cursor=7']:{items:[],cursor:'7',has_more:false},
    '/v1/evolutions':[evolution],[evolutionPath]:evolution,
    [harvestPath]:[harvest],
    [evolutionPath+'/dag']:{nodes:[{kind:'evolution',cost_usd:liveSpend}],edges:[]},
    [evolutionPath+'/conversation?limit=100']:{items:[],cursor:'e:0',has_more:false},
    '/v1/champions':[champion],['/v1/champions/'+champion.id]:champion,
  };
  if(path==='/v1/approvals'&&holdInbox){
    const waiting=holdInbox;
    holdInbox=null;
    await waiting;
  }
  return {ok:true,json:async()=>fixtures[path]||[]};
};
'''
    checks = r'''
const deadline=setTimeout(()=>{throw Error('Portal bootstrap did not settle')},4000);
(async()=>{
  const nextTick=()=>new Promise(setImmediate);
  const waitForStartup=async()=>{while(refreshing)await nextTick()};
  const approve=card=>walk(card).find(node=>node.textContent==='批准');
  await nextTick();
  if(archived){
    assert.deepEqual(calls,['/portal/archive.json']);
    assert.equal($('guest-view-banner').textContent,'只读归档 · 来自仓库中已发布的任务与评测记录');
    assert.equal($('overview').hidden,false);
    assert.deepEqual(links.filter(link=>!link.hidden).map(link=>link.hash),
      ['#/tasks','#/history','#/evals','#/memories','#/evolutions']);
    assert.equal(approvalLink.hidden,true);
    location.hash='#/approvals';
    await listeners.get('hashchange')();
    assert.equal($('approvals-page').hidden,true);
    assert.equal($('overview').hidden,false);
    assert.deepEqual(calls,['/portal/archive.json']);

    $('memory-status').value='active';
    location.hash='#/memories';
    await listeners.get('hashchange')();
    const [memory]=config.archive['/v1/memories'];
    const path='/v1/tasks/'+memory.source_task_id;
    const source=walk($('memory-list')).find(node=>node.href);
    assert.equal(source.href,'#/tasks/'+memory.source_task_id+'/events/'+memory.source_event_seq);
    location.hash=source.href;
    await listeners.get('hashchange')();
    assert.equal($('trace').hidden,false);
    assert.equal($('task-details').hidden,false);
    assert.equal($('task-title').textContent,memory.source_task_id);
    assert.equal(replaySeq,memory.source_event_seq);
    assert.equal($('task-run-workbench').hidden,true);
    assert.equal($('task-conversation').children.length,0);
    const marks=$('swimlane').querySelector('.task-time-marks');
    assert(marks.children.some(node=>/\+\d+\.\d{3} 秒/.test(node.getAttribute('aria-label'))));
    $('trace-next').onclick();
    assert(replaySeq>memory.source_event_seq);
    assert.deepEqual(calls,['/portal/archive.json','/v1/memories?status=active','/v1/tasks?limit=100',
      path,path+'/events?after=0',path+'/artifacts']);
  }else if(destination==='guest'){
    await waitForStartup();
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
    assert.deepEqual(calls.slice(2),['/v1/portal/whoami','/v1/approvals','/v1/projects']);
    assert.equal($('projects-home').hidden,false);
    assert.equal($('guest-view-banner').hidden,true);
    assert.equal($('model-not-configured').hidden,true);
    assert.deepEqual(controls.map(control=>control.hidden),[false,false,true]);
  }else if(destination==='local'){
    await waitForStartup();
    assert.deepEqual(calls,['/v1/portal/config','/v1/approvals','/v1/projects']);
    assert.equal($('projects-home').hidden,false);
    assert.equal(approvalLink.hidden,false);
    assert.equal($('approvals-count').textContent,' (0)');
    assert.equal($('local-access-badge').hidden,false);
    assert.equal($('connect-form').hidden,true);
    assert.equal($('guest-view-banner').hidden,true);
    assert.deepEqual(controls.map(control=>control.hidden),[false,false,false]);
    assert.equal(storage.has('sca-portal-token'),false);

    releaseSuites();
    location.hash='#/overview';
    await route();
    const toggle=$('portal-require-token');
    assert.equal($('portal-access-control').hidden,false);
    assert.equal(toggle.checked,false);
    toggle.checked=true;
    await toggle.onchange();
    assert.equal(toggle.checked,false);
    assert.equal(switchWrites,0);
    assert(warningText.includes('Select-String SCA_EVAL_API_TOKEN .\\.data\\portal.env'));
    assert(warningText.includes('grep SCA_EVAL_API_TOKEN .data/portal.env'));

    acceptWarning=true;
    toggle.checked=true;
    await toggle.onchange();
    assert.equal(switchWrites,1);
    assert.equal($('local-access-badge').hidden,true);
    assert.equal($('connect-form').hidden,false);
    assert.equal($('portal-access-control').hidden,true);
    assert.equal($('guest-view-banner').hidden,false);
    assert.equal(approvalLink.hidden,true);
    assert.equal($('approvals-count').textContent,'');
    await assert.rejects(api('/tasks',{}),/请先登录/);

    $('token').value='fixture-admin';
    await $('connect-form').onsubmit({preventDefault(){}});
    assert.equal(storage.get('sca-portal-token'),'fixture-admin');
    assert.equal($('portal-access-control').hidden,false);
    assert.equal(toggle.checked,true);
    toggle.checked=false;
    await toggle.onchange();
    assert.equal(switchWrites,2);
    assert.equal(storage.get('sca-portal-token'),'fixture-admin');
    assert.deepEqual(authHeaders(),{Authorization:'Bearer fixture-admin'});
  }else if(destination==='projects'){
    assert.deepEqual(calls,['/v1/portal/whoami','/v1/approvals','/v1/projects']);
    const duplicate=route();
    await refresh();
    assert.equal(calls.filter(path=>path==='/v1/projects').length,1);
    releaseProjects();
    await Promise.all([waitForStartup(),duplicate]);
    assert.equal($('projects-home').hidden,false);
    const [card]=$('project-cards').children;
    assert.equal(card.children[0].children[0].children[0].textContent,'默认项目');
    assert.equal(card.children[1].children.at(-1).children[1].textContent,'1,030');
    await refresh();
    await refresh();
    assert.deepEqual(calls,['/v1/portal/whoami','/v1/approvals','/v1/projects']);

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
  }else if(destination==='evolution'){
    await waitForStartup();
    assert.deepEqual(calls,['/v1/portal/config','/v1/evolutions']);
    assert.equal($('evolutions-page').hidden,false);
    assert.deepEqual($('evolution-nav').children.map(node=>node.href),['#/evolutions/runs','#/evolutions/champions']);
    const content=()=>walk($('evolution-content')).map(node=>node.textContent);
    assert(!content().includes('开始演进'));
    location.hash='#/evolutions/runs/'+evolution.id;
    await route();
    assert(walk($('evolution-content')).includes($('evolution-dag')));
    const card=walk($('evolution-content')).find(node=>node.id==='candidate-approval-'+candidate.id);
    assert(walk(card).some(node=>node.textContent==='等待人工审批 · 只读'));
    assert(!['停止演进','批准','拒绝'].some(label=>content().includes(label)));
    assert(calls.includes(evolutionPath+'/dag'));
    assert(calls.includes(evolutionPath+'/conversation?limit=100'));
    assert(content().some(label=>label.includes('已用 $0.0042')));
    liveSpend=.0064;
    await refresh();
    await nextTick();
    assert.equal(calls.filter(path=>path===evolutionPath+'/dag').length,2);
    assert(content().some(label=>label.includes('已用 $0.0064')));

    location.hash='#/evolutions/champions/'+champion.id;
    await route();
    assert.equal(graphsDestroyed,1);
    assert.equal(calls.at(-1),'/v1/champions/'+champion.id);
    assert(content().some(label=>label.startsWith('built ·')));
    assert(!['固化冠军','设置灰度','回滚到父冠军','设为默认版本'].some(label=>content().includes(label)));
    location.hash='#/evolutions/schedules/private-schedule';
    await route();
    assert.equal(calls.at(-1),'/v1/evolutions');
    assert(!calls.some(path=>path.startsWith('/v1/evolution-schedules')));
    location.hash='#/tasks/'+task.id;
    await route();
    assert(calls.includes(taskPath+'/dag'));
    assert(calls.includes(taskPath+'/conversation?limit=100'));
    assert.equal($('task-dag').hidden,false);
    const chat=walk($('task-conversation'));
    const taskCard=chat.find(node=>node.id==='task-approval');
    assert(walk(taskCard).some(node=>node.textContent==='等待人工审核 · 只读'));
    assert(!approve(taskCard));
    assert(chat.find(node=>node.className==='stack conversation-composer').hidden);
    location.hash='#/approvals';
    await route();
    assert.equal(approvalLink.hidden,true);
    assert.equal($('approvals-page').hidden,true);
    assert.equal($('connect-prompt').hidden,false);
    assert.equal($('connect-prompt').querySelector('p').textContent,'请先在概览连接 Portal 工作空间。');
    assert(!calls.includes('/v1/approvals'));
  }else if(taskApproval){
    await waitForStartup();
    assert.equal(approvalLink.hidden,false);
    assert.equal($('approvals-page').hidden,false);
    assert.equal($('approvals-count').textContent,' (1)');
    const entry=walk($('approvals-list')).find(node=>node.href);
    assert.equal(entry.href,approvalHref);
    location.hash=entry.href;
    await route();
    const chat=walk($('task-conversation'));
    const card=chat.find(node=>node.id==='task-approval');
    assert(card.scrolled);
    if(resultApproval){
      assert(walk(card).some(node=>node.textContent==='验证结果 · 通过'));
      assert(walk(card).some(node=>node.textContent==='app.py'));
      const patch=walk(card).find(node=>node.textContent==='查看最终补丁');
      patch.onclick({preventDefault(){}});
      assert($('patch').focused);
      const check=card.querySelectorAll('input')[0];
      check.checked=true;
      check.onchange();
      assert($('task-memory-proposals').querySelectorAll('input')[0].checked);
    }else{
      assert(walk(card).some(node=>node.textContent==='覆盖边界条件'));
    }
    assert(chat.some(node=>node.textContent==='执行了 1 个命令，修改了 1 个文件'));
    assert(chat.some(node=>node.textContent==='Done'));
    assert.equal(chat.filter(node=>node.className==='conversation-receipt').length,1);
    assert.equal(chat.find(node=>node.className==='conversation-receipt').children[0].textContent,'已送达');
    assert.equal($('task-review-chat').href,approvalHref);
    $('task-review-chat').onclick({preventDefault(){}});
    assert(card.focused);
    const feed=chat.find(node=>node.className==='conversation-feed');
    const jump=chat.find(node=>node.className==='conversation-jump');
    Object.assign(feed,{scrollHeight:1000,clientHeight:400,scrollTop:100});
    moreMessages=true;
    await refresh();
    await nextTick();
    assert.equal(feed.scrollTop,100);
    assert.equal(jump.hidden,false);
    jump.onclick();
    assert.equal(feed.scrollTop,1000);
    assert.equal(jump.hidden,true);
    const change=walk(card).find(node=>node.textContent==='要求修改');
    await change.onclick();
    assert.equal(reviewRequests.length,0);
    await approve(card).onclick();
    assert.deepEqual(reviewRequests,[{decision:'approve',feedback:null,accept_memories:resultApproval?['memory']:[]}]);
    assert(walk(card).some(node=>node.textContent==='已批准'));
    await nextTick();
    assert.equal($('approvals-count').textContent,' (0)');
    portalIdentity={tenant_id:'eval',role:'viewer'};
    portalTasks.render({...task,state:'AWAITING_REVIEW'},taskEvents);
    const readOnly=walk($('task-conversation')).find(node=>node.id==='task-approval');
    assert(walk(readOnly).some(node=>node.textContent==='等待人工审核 · 只读'));
    assert(!approve(readOnly));
  }else if(evolutionApproval){
    releaseSuites();
    await waitForStartup();
    assert.equal($('approvals-count').textContent,' (2)');
    const link=href=>walk($('approvals-list')).find(node=>node.href===href);
    location.hash=link(candidateHref).href;
    await route();
    const content=()=>walk($('evolution-content'));
    const card=content().find(node=>node.id==='candidate-approval-'+candidate.id);
    assert(card.scrolled);
    await approve(card).onclick();
    assert.deepEqual(reviewRequests,[{candidate_id:candidate.id}]);
    assert(walk(card).some(node=>node.textContent==='已批准 · 已晋升'));
    location.hash=link(harvestHref).href;
    await route();
    const publication=content().find(node=>node.id==='harvest-approval-'+harvest.id);
    assert(publication.scrolled);
    const publish=walk(publication).find(node=>node.textContent==='批准发布');
    await publish.onclick();
    assert.equal(harvest.status,'pending');
    acceptWarning=true;
    let releaseInbox;
    holdInbox=new Promise(resolve=>{releaseInbox=resolve});
    const staleInbox=portalApprovals.refresh(true);
    try {
      await nextTick();
      await publish.onclick();
      await nextTick();
      assert.deepEqual(reviewRequests,[{candidate_id:candidate.id},{}]);
      const updated=content().find(node=>node.id===publication.id);
      assert(walk(updated).some(node=>node.textContent==='已批准'));
      assert.equal($('approvals-count').textContent,' (0)');
    } finally {
      releaseInbox();
      await staleInbox;
    }
    assert.equal($('approvals-count').textContent,' (0)');
  }else{
    assert.deepEqual(new Set(calls),new Set([
      '/v1/portal/whoami','/v1/approvals',taskPath,taskPath+'/events?after=0',
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
    await waitForStartup();
    assert.equal($('task-run-workbench').hidden,false);
    assert.equal($('task-dag').hidden,false);
    assert(calls.includes(taskPath+'/dag'));
    assert(calls.includes(taskPath+'/conversation?limit=100'));
    assert.equal($('task-picker').disabled,true);
    assert.equal(eventCursor,1);
    assert.equal($('patch').textContent,missingReason);
    assert.equal($('task-missing-artifacts-panel').hidden,false);
    assert.equal($('task-missing-artifacts').children[0].textContent,'verify.log · '+missingReason);
    task.kind='analysis';
    await updateTask();
    assert.equal($('analysis-report-panel').hidden,false);
    assert.equal($('analysis-rendered').children[0].textContent,missingReason);
    assert(!calls.some(path=>path.startsWith(taskPath+'/artifacts/')));
  }
  assert.equal($('error').textContent,'');
})().finally(()=>clearTimeout(deadline)).catch(error=>{console.error(error);process.exitCode=1});
'''
    config = json.dumps({
        "tenant": TENANT_PORTAL_CONFIG, "guest": GUEST_PORTAL_CONFIG,
        "archive": {
            ref: json.loads(body) for ref, (kind, body) in responses.items() if kind == "application/json"
        },
    })
    path = tmp_path / "portal.cjs"
    path.write_text("\n".join([f"const config = {config};", harness, *scripts, script, checks]), encoding="utf-8")
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
    if name in {*ASSETS, "portal-operations.css", "portal-conversation.css", "task-dag.css", "task-flow.js", "task-timeline.js"}:
        kind = "text/javascript" if name.endswith(".js") else "text/css"
        route.fulfill(status=200, content_type=kind, body=(STATIC / name).read_text())
        return True
    return False


@pytest.mark.parametrize("local", [False, True])
def test_portal_browser_tracks_provider_restart_after_writes(local):
    providers_path = '/v1/portal/providers'
    provider = {'id': 'fixture-provider', 'name': 'Local', 'type': 'openai-compatible',
                'base_url': 'http://127.0.0.1:8081/v1', 'models': [{'name': 'local-coder'}]}
    providers = []
    statuses = {'save': 'restarting', 'delete': 'restarting'}

    def handle(route):
        path = urlsplit(route.request.url).path
        if serve_portal(route, path):
            return
        if path.startswith(providers_path):
            assert route.request.headers.get('authorization') == (None if local else 'Bearer fixture-admin')
        if path == providers_path and route.request.method == 'POST':
            providers.append(provider)
            route.fulfill(status=202, json={'provider': provider,
                                           'restart': {'id': 'save', 'status': 'restarting'}})
        elif path == f'{providers_path}/{provider["id"]}' and route.request.method == 'DELETE':
            providers.clear()
            route.fulfill(status=202, json={'restart': {'id': 'delete', 'status': 'restarting'}})
        elif path.startswith(providers_path + '/restarts/'):
            restart_id = path.rsplit('/', 1)[1]
            route.fulfill(json={'id': restart_id, 'status': statuses[restart_id]})
        else:
            fixtures = {
                '/v1/portal/whoami': {'role': 'admin', 'tenant_id': 'eval'},
                '/v1/portal/config': {**TENANT_PORTAL_CONFIG, 'can_configure_model': True, **(
                    {'local_identity': {'role': 'admin', 'tenant_id': 'eval'}, 'portal_require_token': False}
                    if local else {}
                )},
                '/v1/portal/access': {'require_token': False, 'local_no_token_active': True},
                providers_path: {'providers': providers, 'deepseek_models': []},
            }
            route.fulfill(json=fixtures.get(path, []))

    with portal_browser() as browser:
        page = browser.new_page()
        if not local:
            page.add_init_script("localStorage.setItem('sca-portal-token', 'fixture-admin')")
        page.route('**/*', handle)
        page.goto('http://portal.test/portal#/overview')
        panel = page.locator('#model-providers')
        panel.get_by_label('名称', exact=True).fill(provider['name'])
        panel.get_by_label('基础 URL').fill(provider['base_url'])
        panel.get_by_label('平台模型名').fill('local-coder')
        panel.get_by_label('上游模型 ID').fill('coder')
        with page.expect_response(f'**{providers_path}/restarts/save'):
            panel.get_by_role('button', name='保存并重启网关').click()
        panel.get_by_text('配置已保存，正在重启模型网关…', exact=True).wait_for()
        statuses['save'] = 'succeeded'
        panel.get_by_text('配置已保存，模型网关已重启。', exact=True).wait_for()
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response(f'**{providers_path}/restarts/delete'):
            panel.get_by_role('button', name='删除', exact=True).click()
        panel.get_by_text('配置已保存，正在重启模型网关…', exact=True).wait_for()
        statuses['delete'] = 'failed'
        panel.get_by_text('配置已保存，网关重启失败。', exact=False).wait_for()
        panel.get_by_text('尚未添加自定义供应商。', exact=True).wait_for()


def test_portal_browser_guest_evolution_dag_polls_and_links_to_task_history():
    evolution_id, eval_id, task_id, champion_id = "evolution-fixture", "eval-fixture", "task-fixture", "champion-fixture"
    evolution_path = f"/v1/evolutions/{evolution_id}"
    task_path = f"/v1/tasks/{task_id}"
    candidate = {
        "id": "candidate-fixture", "generation": 1, "decision": "pending", "reason": "awaiting approval",
        "hypothesis": "Check boundaries", "overlay": {}, "static_check": {"valid": True},
    }
    row = {"id": evolution_id, "status": "awaiting_approval", "spent_usd": .01, "best_champion_id": champion_id,
           "config": {"suite": "fixture", "agent_version": "fake", "model": "fake", "budget_usd": .1}}
    champion = {"id": champion_id, "candidate_id": candidate["id"], "agent_version": "fake", "model": "fake",
                "parent_champion_id": "previous", "active": True, "metrics": {}}
    graph = {"nodes": [
        {"id": "evolution", "kind": "evolution", "label": "演进", "status": "running"},
        {"id": "evaluate", "kind": "eval_run", "label": "评测 · 开发集", "status": "running",
         "score": .5, "cost_usd": .01, "meta": {"run_id": eval_id, "split": "dev"}},
        {"id": "case", "kind": "case_task", "label": "boundary", "status": "queued",
         "task_id": task_id, "meta": {"parent": "evaluate"}},
    ], "edges": [
        {"from": "evolution", "to": "evaluate", "kind": "sequence"},
        {"from": "evaluate", "to": "case", "kind": "spawn"},
    ]}
    requests = []

    def handle(route):
        path = urlsplit(route.request.url).path
        if serve_portal(route, path):
            return
        requests.append(path)
        assert route.request.method == "GET"
        fixtures = {
            "/v1/portal/config": GUEST_PORTAL_CONFIG,
            "/v1/evolutions": [row], evolution_path: {**row, "candidates": [candidate], "journal": []},
            evolution_path + "/dag": graph,
            "/v1/champions": [champion], f"/v1/champions/{champion_id}": {**champion, "solidify_builds": [{
                "version": "fixture-solidified", "status": "succeeded", "regression_status": "completed",
            }]},
            f"/v1/evals/runs/{eval_id}": {"id": eval_id, "suite": "fixture", "status": "running",
                                         "variant": {"repeat": 1}, "results": [], "result_count": 0},
            task_path: {"id": task_id, "kind": "coding", "state": "SUCCEEDED", "spec": {}, "result": {}},
            task_path + "/dag": {"nodes": [{"id": "task", "kind": "task", "label": "用例任务", "status": "succeeded",
                                           "task_id": task_id}], "edges": []},
        }
        route.fulfill(json=fixtures.get(path, []))

    with portal_browser() as browser:
        page = browser.new_page()
        page.route("**/*", handle)
        page.goto("http://portal.test/portal#/evolutions")
        nav = page.locator('nav[aria-label="主导航"]')
        nav.get_by_role("link", name="自动演进", exact=True).wait_for()
        assert page.get_by_role("button", name="开始演进").count() == 0
        assert page.locator("#evolution-nav").get_by_role("link").count() == 2
        page.locator(f'#evolution-content a[href="#/evolutions/runs/{evolution_id}"]').click()
        panel = page.locator("#evolution-dag")
        assert page.locator('#evolution-conversation form').is_hidden()
        evaluation = panel.locator('[data-node="evaluate"]')
        evaluation.wait_for()
        assert evaluation.get_attribute("aria-expanded") == "false"
        assert "50%" in evaluation.get_attribute("aria-label")
        assert panel.locator('[data-kind="case_task"]').count() == 0
        for label in ("停止演进", "批准", "拒绝"):
            assert page.get_by_role("button", name=label, exact=True).count() == 0

        evaluation.press("Enter")
        case = panel.locator('[data-node="case"]')
        case.wait_for()
        page.wait_for_function("!refreshing && !routeLoad")
        graph["nodes"][2]["status"] = "running"
        page.evaluate("refresh()")
        panel.locator('[data-node="case"][data-status="running"]').wait_for()
        assert evaluation.get_attribute("aria-expanded") == "true"
        assert requests.count(evolution_path + "/dag") >= 2
        panel.get_by_role("link", name="打开评测").click()
        page.get_by_text("fixture / eval-fix", exact=True).wait_for()

        page.goto(f"http://portal.test/portal#/evolutions/runs/{evolution_id}")
        evaluation.press("Enter")
        case.press("Enter")
        panel.get_by_role("link", name="打开用例任务").click()
        page.locator('#task-dag [data-kind="task"]').wait_for()

        page.goto("http://portal.test/portal#/evolutions/champions")
        page.locator(f'#evolution-content a[href="#/evolutions/champions/{champion_id}"]').click()
        page.get_by_text("fixture-solidified", exact=False).wait_for()
        for label in ("固化冠军", "回滚到父冠军", "设为默认版本"):
            assert page.get_by_role("button", name=label, exact=True).count() == 0
        assert page.get_by_role("link", name="设置灰度").count() == 0
        assert not any(path.startswith("/v1/evolution-schedules") or "rollouts" in path for path in requests)


def test_portal_browser_launches_evolution_and_steers_through_the_conversation():
    evolution_id = 'evolution-fixture'
    path = f'/v1/evolutions/{evolution_id}'
    root, generation, run = f'evolution:{evolution_id}', 'generation:1', 'eval:baseline'
    at = '2026-11-22T00:00:00Z'
    row = {'id': evolution_id, 'status': 'running', 'spent_usd': .01, 'created_at': at,
           'config': {'suite': 'dsh-smoke', 'agent_backend': 'dsh', 'model': 'model-a', 'budget_usd': 1},
           'candidates': [], 'journal': []}
    graph = {'nodes': [
        {'id': root, 'kind': 'evolution', 'label': '演进', 'status': 'running', 'cost_usd': .0142,
         'meta': {'band': 0, 'level': 0, 'column': 0}},
        {'id': run, 'kind': 'eval_run', 'label': '基线评测', 'status': 'succeeded', 'score': 1,
         'meta': {'band': 0, 'level': 1, 'column': 1, 'cases_passed': 1}},
        {'id': 'case', 'kind': 'case_task', 'label': '用例一', 'status': 'succeeded',
         'meta': {'band': 0, 'level': 2, 'column': 1, 'parent': run}},
        {'id': generation, 'kind': 'generation', 'label': '第 1 代', 'status': 'running',
         'meta': {'band': 1, 'level': 0, 'column': 0}},
    ], 'edges': [{'from': start, 'to': end, 'kind': 'sequence'} for start, end in (
        (root, run), (run, 'case'), ('case', generation),
    )]}
    journal = [{'id': 'baseline', 'kind': 'evaluation', 'text': '基线评测结果 · 1/1 通过 · $0.01',
                'created_at': at, 'dag_node_id': run, 'evaluation': {
                    'role': 'baseline', 'split': 'dev', 'status': 'completed', 'passed': 1, 'total': 1,
                    'cost_usd': .01,
                }}]
    starts, messages, cursors = [], [], []

    def handle(route):
        url = urlsplit(route.request.url)
        if serve_portal(route, url.path):
            return
        if url.path == '/v1/evolutions' and route.request.method == 'POST':
            starts.append(route.request.post_data_json)
            route.fulfill(status=201, json={'id': evolution_id})
        elif url.path == path + '/messages':
            body = route.request.post_data_json
            messages.append(body)
            mode = body['mode']
            if mode == 'pause':
                row['status'] = 'paused'
            elif mode == 'resume':
                row['status'] = 'running'
            if mode == 'guidance':
                journal.extend([
                    {'id': 'hint', 'kind': 'message', 'text': body['text'], 'actor': 'operator', 'mode': 'guidance',
                     'message_id': 'hint',
                     'created_at': at, 'dag_node_id': root},
                    {'id': 'applied', 'kind': 'receipt', 'text': '已应用于第 1 代提议',
                     'created_at': at, 'dag_node_id': generation, 'message_id': 'hint', 'status': 'applied'},
                ])
            route.fulfill(status=202, json={})
        elif url.path == path + '/conversation':
            match = re.search(r'cursor=(\d+)', url.query)
            offset = int(match[1]) if match else 0
            cursors.append(offset)
            route.fulfill(json={'items': journal[offset:], 'cursor': str(len(journal)), 'has_more': False})
        else:
            fixtures = {
                '/v1/portal/whoami': {'role': 'admin', 'tenant_id': 'eval'},
                '/v1/portal/config': {**TENANT_PORTAL_CONFIG, 'default_agent_backend': 'dsh'},
                '/v1/models': {'default_model': 'model-b', 'evolve_model': 'proposer-a',
                               'data': [{'id': 'model-b', 'unpriced': True}, {'id': 'proposer-a'}]},
                '/v1/agents/opencode/versions': [{'version': 'fixture-opencode'}],
                '/v1/evals/suites': [
                    {'name': 'dsh-smoke', 'cases': ['a', 'b', 'c'], 'case_metadata': {
                        'a': {'split': 'dev'}, 'b': {'split': 'dev'}, 'c': {'split': 'holdout'},
                    }},
                    {'name': 'opencode-evolve', 'cases': ['a', 'b'], 'case_metadata': {
                        'a': {'split': 'dev'}, 'b': {'split': 'holdout'},
                    }},
                    {'name': 'dev-only', 'cases': ['a'], 'case_metadata': {'a': {'split': 'dev'}}},
                ],
                path: row, path + '/dag': graph,
            }
            route.fulfill(json=fixtures.get(url.path, []))

    with portal_browser() as browser:
        page = browser.new_page(viewport={'width': 1400, 'height': 950})
        page.add_init_script("localStorage.setItem('sca-portal-token', 'fixture-admin')")
        page.route('**/*', handle)
        page.goto('http://portal.test/portal#/evolutions')
        start = page.get_by_role('button', name='开始演进', exact=True)
        start.wait_for()
        form = page.locator('#evolution-content form')
        backend, suite = form.get_by_label('Agent 后端'), form.get_by_label('评测套件')
        assert backend.input_value() == 'dsh' and suite.input_value() == 'dsh-smoke'
        assert suite.locator('option').all_text_contents() == [
            'dsh-smoke · 开发集 2 · 留出集 1', 'opencode-evolve · 开发集 1 · 留出集 1',
        ]
        backend.select_option('opencode')
        assert suite.input_value() == 'opencode-evolve'
        assert form.get_by_label('基线版本').input_value() == 'fixture-opencode'
        backend.select_option('dsh')
        assert form.get_by_label('评测模型').input_value() == 'model-b'
        assert form.get_by_label('反思与候选生成模型').input_value() == 'proposer-a'
        assert page.locator('#models option').evaluate_all('(options) => options.map(o => o.value)') == [
            'model-b', 'proposer-a',
        ]
        assert form.get_by_text('未定价模型按 $0 计费', exact=False).is_visible()
        form.get_by_label('总预算（美元）').fill('0.5')
        start.click()
        page.wait_for_url(f'**/portal#/evolutions/runs/{evolution_id}')
        assert {key: starts[0][key] for key in ('agent_backend', 'suite', 'model', 'evolve_model', 'generations', 'candidates',
                                                'repeat', 'budget_usd')} == {
            'agent_backend': 'dsh', 'suite': 'dsh-smoke', 'model': 'model-b', 'evolve_model': 'proposer-a', 'generations': 1,
            'candidates': 2, 'repeat': 1, 'budget_usd': .5,
        }
        panel, chat = page.locator('#evolution-dag'), page.locator('#evolution-conversation')
        panel.locator('[data-band="基线"]').wait_for()
        for node in (root, generation):
            label = panel.locator(f'[data-node="{node}"]').get_attribute('aria-label')
            assert '进行中' in label
            assert '通过率 100%' not in label
        page.get_by_text('已用 $0.0142', exact=False).wait_for()
        assert '1 个用例 · 1 通过' in panel.locator(f'[data-node="{run}"]').get_attribute('aria-label')
        assert chat.bounding_box()['x'] > panel.bounding_box()['x']
        chat.get_by_role('link', name='定位节点').click()
        assert panel.locator(f'[data-node="{run}"]').get_attribute('aria-pressed') == 'true'
        assert panel.locator(f'[data-node="{run}"]').get_attribute('aria-expanded') == 'false'
        prompt = chat.get_by_role('textbox', name='给下一代提议的指引')
        prompt.fill('留意边界条件')
        prompt.press('Enter')
        chat.get_by_text('已应用于第 1 代提议', exact=True).wait_for()
        assert chat.locator('.conversation-user').get_by_text('留意边界条件').is_visible()
        assert messages[-1] == {'text': '留意边界条件', 'mode': 'guidance'}
        assert 1 in cursors
        chat.locator('.conversation-user').get_by_role('link', name='已应用于第 1 代提议', exact=True).click()
        assert panel.locator(f'[data-node="{generation}"]').get_attribute('aria-pressed') == 'true'
        chat.get_by_role('button', name='暂停', exact=True).click()
        chat.get_by_role('button', name='恢复', exact=True).wait_for(timeout=6000)
        chat.get_by_role('button', name='恢复', exact=True).click()
        assert [msg['mode'] for msg in messages] == ['guidance', 'pause', 'resume']
        page.set_viewport_size({'width': 820, 'height': 850})
        assert chat.bounding_box()['y'] < panel.bounding_box()['y']
        row['status'] = 'stopped'
        chat.get_by_text('演进已结束，无法发送消息。').wait_for(timeout=6000)
        assert prompt.is_disabled()


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
            "/v1/approvals": {"count": 0, "items": []},
            "/v1/projects": [project],
            task_path: {"id": task_id, "kind": "coding", "state": "SUCCEEDED", "spec": {}, "result": {}},
            task_path + "/conversation": {"items": [], "cursor": "0", "has_more": False},
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
            page.get_by_text("(0)", exact=True).wait_for(timeout=3000)
            page.evaluate("refresh()")
            assert requests[0] == "/v1/portal/whoami"
            assert sorted(requests[1:]) == ["/v1/approvals", "/v1/projects"]
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
