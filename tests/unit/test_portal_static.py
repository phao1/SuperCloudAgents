import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


class PortalParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == 'id':
                self.ids.append(value)
            if key in ('src', 'href', 'action'):
                self.urls.append(value)


def test_portal_is_self_contained_and_has_valid_references():
    source = Path('src/sca/api/static/portal.html').read_text()
    parser = PortalParser()
    parser.feed(source)
    parser.close()
    assert len(parser.ids) == len(set(parser.ids))
    assert set(re.findall(r"\$\('([^']+)'\)", source)) <= set(parser.ids)
    assert all(not urlsplit(url).scheme and not urlsplit(url).netloc for url in parser.urls)
    assert 'fetch(\'/v1\'+path' in source
    assert 'innerHTML' not in source
    assert 'Authorization' in source and 'localStorage' in source
    assert '<html lang="zh-CN">' in source


def test_portal_workbench_sections_and_safe_rendering():
    source = Path('src/sca/api/static/portal.html').read_text()
    parser = PortalParser()
    parser.feed(source)
    assert {'stepper', 'evaluation', 'matrix', 'trace', 'timeline', 'loop',
            'instruction', 'start-fix', 'build-log', 'comparison', 'versions'} <= set(parser.ids)
    assert len(source.encode()) < 200 * 1024
    assert 'prefers-color-scheme:dark' in source
    assert '<textarea id="instruction"' in source
    assert 'extra_instruction:' in source
    assert 'r.status===401||r.status===403' in source
    assert '非 ASCII 字符丢失' in source
    assert 'acceptance.completed' in source
    assert not re.search(r'https?://|(?:src|href)=["\']//', source)
    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|document.write|eval\(', source)
    assert "localStorage.getItem('sca-portal-flow')" not in source
    assert "data('/tasks?limit=500')" in source
    assert "data('/agents/opencode/builds')" in source


def test_portal_diff_and_metrics_runtime(tmp_path):
    import shutil
    import subprocess

    import pytest

    node = shutil.which('node') or str(Path.home() / '.local/bin/node')
    if not Path(node).exists():
        pytest.skip('Node is unavailable')
    source = Path('src/sca/api/static/portal.html').read_text()
    script = source.split('<script>')[1].split('</script>')[0]
    script = script[:script.index("$('start-fix').onclick")]
    harness = r'''
const assert = require('node:assert/strict');
const flowModel = require(process.argv[2]);
const {buildRecords, activeTiming} = require(process.argv[3]);
class Element {
 constructor(){this.children=[];this.dataset={};this.textContent='';this.className='';this.value='';
 this.classList={add: name=>{this.className+=' '+name}}}
 append(...items){this.children.push(...items)}
 replaceChildren(...items){this.children=items}
 querySelectorAll(){return []}
}
const nodes=new Map();
const document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)},createElement:()=>new Element()};
const localStorage={getItem:()=>''};
'''
    checks = r'''
const target=new Element();
assert.equal(terminalLines('clone\r\nprogress 1%\rprogress 100%\r\nend'), 'clone\nprogress 100%\nend');
assert.equal(terminalLines('long progress\rdone\r'), 'done');
assert.equal(uniqueDiagnostics([{title:'rule',note:'case · 第 1 次'},{title:'rule',note:'case · 第 1 次 · evidence'},{title:'rule',note:'case · 第 2 次'},{title:'other',note:'case · 第 1 次'}]).length,3);
const timing=activeTiming(buildRecords([
 {type:'agent.usage',payload:{started_at_ms:0,ended_at_ms:40}},
 {type:'agent.tool',payload:{started_at_ms:20,ended_at_ms:60}},
 {type:'agent.command',payload:{started_at_ms:80,ended_at_ms:100}},
 {type:'acceptance.completed',created_at:'2026-01-01T00:00:00Z'},
]));
assert.equal(timing.extent,100);
assert.equal(timing.model,40);
assert.equal(timing.tools,40);
assert.equal(timing.other,20);
assert.equal(activeTiming([]),null);
assert.equal(activeTiming(buildRecords([
 {type:'agent.usage',payload:{started_at_ms:100,ended_at_ms:500,model_call_duration_ms:50}},
])).extent,400);

assert(nonAsciiLost('@@\n-café\n+cafe'));
assert(!nonAsciiLost('@@\n-你好\n+你好！'));
assert.equal(diagnostics([{seq:1,type:'agent.message',payload:{text:'The edit tool stripped the Chinese content'}}]).length,1);
assert.equal(diagnostics([{seq:1,type:'verification.completed',payload:{passed:true}},{seq:2,type:'acceptance.completed',payload:{passed:false}}])[0].title,'△ 隐藏检查缺口');
assert.equal(diagnostics([{type:'agent.command',payload:{exit_code:-1}}]).length,1);
assert.equal(diagnostics([{type:'agent.command',payload:{exit_code:0}}]).length,0);
const repeated=[{case_id:'a',repeat:1,acceptance_passed:true,duration_s:2,cost_usd:.01,tokens:10},{case_id:'a',repeat:2,acceptance_passed:false,duration_s:4,cost_usd:.02,tokens:20}];
assert.equal(dimensions(repeated).flaky,1);
assert.equal(dimensions(repeated).spread,'100.0 pp');
assert.equal(dimensions(repeated).p50,3);
assert.equal(dimensions([]).tokens,'—');
const oldRun={suite:'x',suite_hash:'hash',status:'completed',variant:{repeat:1},results:[{case_id:'a',repeat:0,acceptance_passed:false,verification_passed:true}]};
const processFailure={case_id:'a',repeat:0,acceptance_passed:true,trajectory_passed:false,verification_passed:true};
assert.equal(overallPassed(processFailure),false);
assert.equal(resultLabel(processFailure),'⚠ 验收通过 · 过程未通过');
assert.equal(dimensions([processFailure]).rate,'0%');
assert.equal(dimensions([processFailure]).acceptanceRate,'100%');
assert.equal(metrics([processFailure]).rate,'0%');
assert.equal(caseChange([{...processFailure,trajectory_passed:true}],[processFailure]),'✕ 回归');
const findingRows=trajectoryItems({...processFailure,task_id:'task',trajectory_findings:[{rule_id:'tool_output_integrity',event_seq:7,message:'<script>'},{rule_id:'tool_output_integrity',event_seq:7,message:'second'}]});
assert.equal(uniqueDiagnostics(findingRows).length,2);
assert.equal(findingRows[0].href,'#/tasks/task/events/7');
assert(findingRows[0].note.includes('<script>'));
assert.equal(trajectoryItems({}).length,0);
assert.equal(trajectoryItems({trajectory_findings:[{rule_id:'future',event_seq:1}]})[0].title,'⚠ 过程检查 · 过程规则未通过');
const fixedRun={...oldRun,results:[{case_id:'a',repeat:0,acceptance_passed:true,verification_passed:true}]};
assert(verdictFor(oldRun,fixedRun).adopt);
assert(!verdictFor(fixedRun,{...fixedRun,results:[processFailure]}).adopt);
assert(verdictFor(fixedRun,{...fixedRun,results:[processFailure]}).reason.includes('过程回归信号'));
canRescore=true;
updateRescoreButton(fixedRun);
assert.equal(nodes.get('rescore').hidden,false);
updateRescoreButton({...fixedRun,status:'running'});
assert.equal(nodes.get('rescore').hidden,true);
canRescore=false;
updateRescoreButton(fixedRun);
assert.equal(nodes.get('rescore').hidden,true);
assert(!verdictFor(fixedRun,oldRun).adopt);
assert.equal(verdictFor(fixedRun,oldRun).regressions,1);
assert(!verdictFor(oldRun,{...fixedRun,status:'running'}).adopt);
assert(!verdictFor(oldRun,{...fixedRun,suite_hash:'other'}).adopt);
assert(!verdictFor(oldRun,{...fixedRun,variant:{repeat:2}}).adopt);
assert(!verdictFor(oldRun,{...fixedRun,results:[]}).adopt);
const {steps,tracks}=flowModel.trace({spec:{plan:'auto'}},[
 {seq:1,type:'task.created'},
 {seq:2,type:'plan.started',payload:{attempt:1}},
 {seq:3,type:'plan.completed',payload:{split:true,subtasks:[{name:'api'},{name:'ui'}]}},
 {seq:4,type:'subtask.started',payload:{subtask:'api'}},
 {seq:5,type:'sandbox.created',payload:{subtask:'api'}},
 {seq:6,type:'agent.turn_started',payload:{subtask:'api',turn:1}},
 {seq:7,type:'agent.command',payload:{subtask:'api',turn:1}},
 {seq:8,type:'intents.checked',payload:{subtask:'api',turn:1,checks:[{passed:false}]}},
 {seq:9,type:'verification.completed',payload:{subtask:'api',turn:1,passed:true}},
 {seq:10,type:'sandbox.restored',payload:{subtask:'api'}},
 {seq:11,type:'agent.turn_started',payload:{subtask:'api',turn:2}},
 {seq:12,type:'verification.completed',payload:{subtask:'api',turn:2,passed:true}},
 {seq:13,type:'subtasks.merged',payload:{subtasks:[{name:'api',status:'merged'}]}},
]);
assert.equal(tracks.get('child:api'),'子任务 · api');
assert(steps.find(step=>step.seq===3).edges.includes('planner>decouple'));
assert(steps.find(step=>step.seq===5).edges.includes('subtask_q>sandbox_sched'));
const retry=steps.find(step=>step.seq===11);
assert.equal(retry.turn,2);
assert(retry.edges.includes('verify_gw>optim_entry'));
assert(retry.edges.includes('recover_gate>runtime'));
const projection=flowModel.project(steps,steps.indexOf(retry));
assert.equal(projection.nodes.find(n=>n.id==='runtime').state,'current');
assert.equal(projection.nodes.find(n=>n.id==='executor').state,'seen');
assert([...projection.nodes,...projection.edges].every(entry=>!('color' in entry)));
assert.equal(projection.edges.find(e=>e.id==='optim_entry>runtime').state,'current');
assert(!projection.edges.some(e=>e.id==='executor>dag'));
const single=flowModel.trace({spec:{plan:'none'}},[{seq:1,type:'task.created'},{seq:2,type:'sandbox.created'}]);
assert(!flowModel.project(single.steps,1).nodes.some(node=>node.id==='planner'||node.id==='decouple'));
const planEvent={seq:33,type:'plan.completed',payload:{split:true,reason:'Separate files',subtasks:[
 {name:'validation',paths:['orders/validation.py'],instruction:'Check bounds'},
 {name:'totals',paths:['orders/totals.py'],instruction:'Sum by currency'},
 {name:'report',paths:['orders/report.py'],instruction:'Export CSV'},
]}};
const planDetails=flowModel.describeStep({spec:{plan_review:true}},planEvent);
assert.equal(planDetails.items.map(item=>item.title).join(','),'validation,totals,report');
assert.equal(planDetails.items[2].detail,'orders/report.py');
assert(planDetails.facts.some(fact=>fact.value.includes('并行执行')));
assert(planDetails.facts.some(fact=>fact.value.includes('获批')));
const childDetails=flowModel.describeStep({},{seq:40,type:'subtask.started',payload:{subtask:'report'}},[planEvent]);
assert(childDetails.facts.some(fact=>fact.value==='validation、totals'));
const sandboxEvent={seq:43,type:'sandbox.created',payload:{driver:'docker',sandbox_id:'sca-task-one',
 state_transport:'mount',image:'sca-sandbox:latest',cpus:1.5,memory_mb:1536}};
const sandboxDetails=flowModel.describeStep({},sandboxEvent,[],{sandbox_driver:'k8s'});
assert(sandboxDetails.facts.some(fact=>fact.label==='运行方式'&&fact.value==='Docker 容器'));
assert(sandboxDetails.facts.some(fact=>fact.value==='挂载工作目录'));
const restoredDetails=flowModel.describeStep({},{seq:80,type:'sandbox.restored',payload:{sandbox_id:'sca-task-one'}},[sandboxEvent]);
assert(restoredDetails.facts.some(fact=>fact.value==='sca-sandbox:latest'));
const oldSandbox=flowModel.describeStep({},{seq:43,type:'sandbox.created',payload:{sandbox_id:'old'}},[],{sandbox_driver:'docker'});
assert(oldSandbox.facts.some(fact=>fact.label==='当前环境'&&fact.value==='Docker 容器'));
const emptyMemory=flowModel.describeStep({result:{memory_context:'later context'}},{type:'memory.injected',payload:{ids:[],method:'all'}});
assert(emptyMemory.facts.some(fact=>fact.value==='0 条')&&!emptyMemory.note);
const selectedMemory=flowModel.describeStep({result:{memory_context:'run the full suite'}},{type:'memory.injected',payload:{ids:['one'],method:'vector'}});
assert(selectedMemory.facts.some(fact=>fact.value==='.sca/context/MEMORY.md'));
assert.equal(selectedMemory.note,'run the full suite');
const before={
 ...fixedRun,suite:'opencode-prompt',
 variant:{repeat:2,variant:{agent_version:'2.3.0-sca.1',agent_backend:'opencode'}},
 results:[0,1].map(repeat=>({...fixedRun.results[0],repeat,tokens:100,cost_usd:.01})),
};
const after={...before,variant:{repeat:2,variant:{agent_version:'2.3.0-sca.2',agent_backend:'opencode'}},
 results:before.results.map(row=>({...row,tokens:80,cost_usd:.009}))};
portalConfig={prompt_evolution:{suite:'opencode-prompt'}};
assert(!verdictFor(before,after).adopt);
versions=[{version:'2.3.0-sca.2',base_version:'2.3.0-sca.1',system_prompt_sha256:'candidate'}];
assert(verdictFor(before,after).adopt);
assert(!verdictFor(before,{...after,results:after.results.slice(0,1)}).adopt);
portalConfig=null;
const records=buildRecords([
 {seq:1,type:'task.created'},
 {seq:2,type:'agent.usage',payload:{started_at_ms:100,ended_at_ms:500,model_call_duration_ms:50}},
 {seq:3,type:'verification.completed',created_at:'2026-01-01T00:00:00Z',payload:{passed:false}},
 {seq:4,type:'agent.command',payload:{is_error:true,duration_ms:42,ended_at_ms:300}},
 {seq:5,type:'agent.message',created_at:'2026-01-01T00:00:00Z',payload:{duration_ms:30}},
]);
assert.deepEqual(records.map(record=>record.kind),['input','model','tool','tool','model']);
assert.equal(records[0].interval,null);
assert.deepEqual(records[1].interval,[100,150]);
assert.equal(records[2].interval[0],records[2].interval[1]);
assert(records[2].failed && records[3].failed);
assert.deepEqual(records[3].interval,[258,300]);
assert.equal(records[4].interval[0],records[4].interval[1]);

diff(target,'@@\n-你好，世界\n+hello world\n context');
assert(target.children.some(n=>n.className.includes('warning')));
diff(target,'@@\n-你好\n+你好！');
assert(!target.children.some(n=>n.className.includes('warning')));
diff(target,'@@\n-你好\n context\n+你好');
assert(target.children.some(n=>n.className.includes('warning')));
diff(target,'@@\n-<script>alert(1)</script>\n+safe');
assert.equal(target.children[1].textContent,'-<script>alert(1)</script>');
assert.equal(extractPatches({metadata:{files:[{diff:'@@\n-old\n+new'}]}}).length,1);
assert.equal(extractPatches({input:{filePath:'hello.ts',oldString:'你好',newString:'hi'}}).length,1);
assert.equal(metrics([]).rate,'—');
assert.equal(metrics([{acceptance_passed:true,cost_usd:.01,duration_s:2,turns:1},
 {acceptance_passed:false,cost_usd:.02,duration_s:4,turns:3}]).rate,'50%');
assert.equal(metrics([{cost_usd:.02,duration_s:4}]).cost,'$0.0200');
taskEvents=[
 {seq:1,type:'agent.turn_started',payload:{turn:1}},
 {seq:2,type:'agent.command',payload:{turn:1,command:'bun test',exit_code:0,output_tail:'1 pass'}},
 {seq:3,type:'agent.usage',payload:{turn:1,input_tokens:20,output_tokens:10,cost:.01}},
 {seq:4,type:'agent.turn_started',payload:{turn:2}},
 {seq:5,type:'acceptance.completed',payload:{passed:false,exit_code:1,output_tail:'中文断言失败'}}
];
renderTimeline();
assert.equal(nodes.get('timeline').children.length,2);
assert(nodes.get('timeline').children[0].children[1].textContent.includes('20'));
assert(nodes.get('timeline').children[1].children.at(-1).open);
(async()=>{
 const old='11111111-1111-1111-1111-111111111111';
 const failed='22222222-2222-2222-2222-222222222222';
 runs=[{id:old,suite:'opencode-demo'},
 {id:'new',created_at:'2026-01-02',suite:'opencode-demo',variant:{variant:{agent_version:'2.3.0-sca.2'}}}];
 data=async path=>path.endsWith('/fixes')?[]:path.startsWith('/tasks')?[{id:'fix',spec:{push:true,
 instruction:'You are working on the source code of OpenCode.\nFailing task: '+failed+'\nEvaluation: '+old}}]:[{id:'build',created_at:'2026-01-01',task_id:'fix',version:'2.3.0-sca.2'}];
 await recoverFlow();
 assert.equal(flow.fixTask,'fix');
 assert.equal(flow.oldRun,old);
 assert.equal(flow.failedTask,failed);
 assert.equal(flow.build,'build');
 assert.equal(flow.newRun,'new');
 data=async path=>path.endsWith('/fixes')?[]:path.startsWith('/tasks')?[{id:'label-fix',spec:{labels:{source_task_id:failed,eval_run_id:old},instruction:'',push:true}}]:[];
 await recoverFlow();
 assert.equal(flow.fixTask,'label-fix');
 assert.equal(flow.oldRun,old);
 data=async path=>path.endsWith('/fixes')?[{id:'api-fix',labels:{source_task_id:failed,eval_run_id:old,source_version:'2.3.0'},builds:[{id:'api-build',created_at:'2026-01-01',version:'2.3.0-sca.2'}]}]:[];
 await recoverFlow();
 assert.equal(flow.fixTask,'api-fix');
 assert.equal(flow.oldRun,old);
 assert.equal(flow.build,'api-build');
 assert.equal(flow.newRun,'new');
 runs[1].created_at='2025-12-31';
 await recoverFlow();assert.equal(flow.newRun,undefined);
 data=async()=>[];
 await recoverFlow();
 assert.deepEqual(flow,{});
 canRescore=true;activeRun='run';
 const originalMatrix=matrix;matrix=async()=>{};
 data=async(path,body)=>{assert.equal(path,'/evals/runs/run/rescore');assert.deepEqual(body,{});return fixedRun};
 await rescoreCurrentRun();assert.equal(runDetails.get('run'),fixedRun);matrix=originalMatrix;
 data=async()=>{const e=Error('forbidden');e.status=403;throw e};
 await rescoreCurrentRun();
 assert.equal(canRescore,false);
 assert.equal($('error').textContent,'');
 global.fetch=async()=>({status:403});
 await assert.rejects(api('/portal/config'),/权限不足/);
 global.fetch=async()=>({status:401});
 await assert.rejects(api('/portal/config'),/认证失败/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path = tmp_path / 'portal.cjs'
    path.write_text(harness + script + checks)
    flow_model = Path('src/sca/api/static/task-flow.js').resolve()
    timeline = Path('src/sca/api/static/task-timeline.js').resolve()
    result = subprocess.run([node, str(path), str(flow_model), str(timeline)],
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


def test_portal_routes_evidence_and_themes():
    source = Path('src/sca/api/static/portal.html').read_text()
    for route in ('overview', 'evals', 'runs', 'compare', 'tasks', 'loop', 'versions'):
        assert route in source
    for feature in ('swimlane', 'evidence', 'run-diagnostics', 'task-diagnostics',
                    'run-metrics', 'task-metrics', 'theme', 'architecture-progress'):
        assert f'id="{feature}"' in source
    assert 'model_call_duration_ms' in source
    assert 'r.repeat===i' in source
    assert "Number(r.repeat)+1" in source
    assert 'source_task_id' in source and 'eval_run_id' in source
    assert "window.addEventListener('hashchange'" in source
    assert 'data-theme=dark' in source
    assert 'keydown' in source and 'Escape' in source
