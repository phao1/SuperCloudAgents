import re
from html.parser import HTMLParser
from pathlib import Path

PORTAL = Path('src/sca/api/static/portal.html')


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for key, value in attrs if key == 'id')


def test_memory_and_analysis_navigation_and_references():
    source = PORTAL.read_text()
    parser = IdParser()
    parser.feed(source)
    assert len(parser.ids) == len(set(parser.ids))
    assert set(re.findall(r"\$\('([^']+)'\)", source)) <= set(parser.ids)
    for route, title in [('memories', '共享记忆'), ('analysis', '数据分析')]:
        assert f'href="#/{route}"' in source
        assert f"{route}:['{route}-page','{title}'" in source


def test_memory_endpoints_and_api_statuses():
    source = PORTAL.read_text()
    assert "data('/memories?status='" in source
    assert "'&scope='+enc(scope)" in source
    assert "await data('/memories',{text:" in source
    assert "memory.status==='active'?'retired':'active'},'PATCH')" in source
    assert "await api('/memories/'+enc(memory.id),undefined,'DELETE')" in source
    assert "if(!confirm('确认删除这条共享记忆？'))return" in source
    for status in ('active', 'proposed', 'retired'):
        assert f'value="{status}"' in source
    for field in ('memory.scope', 'memory.category', 'memory.source_task_id', 'memory.last_used_at'):
        assert field in source


def test_review_accepts_only_selected_proposals_on_approval():
    source = PORTAL.read_text()
    tasks = PORTAL.with_name('portal-tasks.js').read_text()
    assert "task?.state==='AWAITING_REVIEW'?(task.result?.memory_proposals||[]):[]" in source
    assert "rows.filter(row=>ids.includes(row.id))" in source
    assert "check.type='checkbox'" in source
    assert 'choices.add(memory.id)' in source and 'choices.delete(memory.id)' in source
    assert 'target.hidden=!ids.length' in source
    assert "if (decision === 'approve') body.accept_memories = checkedMemories(task.id)" in tasks
    assert "accept_memories:checkedMemories(flow.fixTask)" in source
    assert "await renderMemoryProposals(task,'loop-memory-proposals')" in source
    assert "portalTasks.submitReview(task,decision" in source
    assert "await api(taskPath(task) + '/review', body)" in tasks


def test_memory_and_report_content_uses_text_nodes():
    source = PORTAL.read_text()
    assert "el('span',memory.text)" in source
    assert 'n.textContent=text' in source
    assert "$('analysis-report').textContent=report" in source
    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|document.write', source)


def test_analysis_upload_submission_and_report_reuse_task_view():
    source = PORTAL.read_text()
    assert 'accept=".csv,.json" multiple required' in source
    assert "new FormData();form.append('file',file)" in source
    assert "fetch('/v1/files',{method:'POST',headers:{Authorization:'Bearer '+portalToken}" in source
    assert 'inputs.push((await response.json()).id)' in source
    assert "kind:'analysis',spec:{question,inputs,report_format:'markdown',checks}" in source
    assert 'await openTask(task.id)' in source
    assert 'await portalTasks.report(t,artifacts)' in source
    tasks = PORTAL.with_name('portal-tasks.js').read_text()
    assert "api(taskPath(task) + '/artifacts/report.md')" in tasks
    assert 'panel.hidden=!report' in source


def test_setup_builds_analysis_image():
    source = Path('scripts/portal_demo.sh').read_text()
    assert 'run "$SCA_DOCKER_BIN" build -t sca-sandbox-analysis:latest deploy/sandbox-analysis' in source
