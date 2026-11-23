import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from test_portal_phase6_static import TENANT_PORTAL_CONFIG, portal_browser, serve_portal

NOW = datetime(2026, 11, 24, 8, tzinfo=UTC)


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat()


def snapshot(state, operation, *, age=8, finished=False):
    return {
        'state': state, 'running': not finished, 'finished': finished, 'as_of': ago(0),
        'started_at': ago(210), 'elapsed_s': 210,
        'phase': {'operation': operation, 'started_at': ago(108)},
        'last_activity_at': ago(age), 'stalled': age > 120 and not finished, 'stall_warn_s': 120,
        'agents': [], 'cost_usd': .003,
    }


def screenshot(page, panel, path):
    panel.evaluate("(node) => node.scrollIntoView({block: 'center'})")
    page.screenshot(path=path, animations='disabled')


@pytest.mark.parametrize(('width', 'height'), [(1440, 1000), (390, 844)])
def test_portal_browser_live_task_timers_actions_and_summary(tmp_path, width, height):
    path = '/v1/tasks/task-live-fixture'
    task = {'id': 'task-live-fixture', 'kind': 'coding', 'state': 'RUNNING',
            'spec': {'agent_backend': 'dsh'}, 'result': {}, 'created_at': ago(210), 'updated_at': ago(8)}
    live = snapshot('RUNNING', 'subtasks')
    lane = {'label': 'Agent 1', 'backend': 'dsh', 'subtask': 'e', 'turn': 1, 'tokens': 1240, 'cost_usd': .003,
            'activity': {'operation': 'model', 'started_at': ago(48)}, 'last_activity_at': ago(8)}
    live['agents'] = [lane, {'label': 'Agent 2', 'backend': 'dsh', 'subtask': 'b',
                            'activity': {'operation': 'dependency', 'detail': 'e', 'started_at': ago(108)}}]
    items = [
        {'id': 'setup', 'kind': 'setup.completed', 'text': '依赖与环境准备完成', 'created_at': ago(200),
         'duration_s': 5.2},
        {'id': 'plan', 'kind': 'plan.completed', 'text': '规划完成', 'created_at': ago(110), 'duration_s': 31},
        {'id': 'operations', 'kind': 'operations', 'text': '执行详情', 'backend': 'dsh', 'created_at': ago(40),
         'activity': {'turn_seq': 1, 'files': [], 'operations': [
             {'kind': 'command', 'name': 'python -m pytest -q', 'exit_code': 1, 'duration_s': 4.1, 'failed': True}
         ]}},
        {'id': 'verify', 'kind': 'verification.completed', 'text': '验证失败', 'created_at': ago(30),
         'duration_s': 4.1, 'output_tail': 'AssertionError: expected 2, got 1'},
    ]
    polls = []
    launched = time.monotonic()

    def handle(route):
        request = urlsplit(route.request.url)
        if serve_portal(route, request.path):
            return
        if request.path == path + '/live':
            polls.append(live['state'])
            live['as_of'] = ago(launched - time.monotonic())
        route.fulfill(json={
            '/v1/portal/whoami': {'role': 'member', 'tenant_id': 'eval'},
            '/v1/portal/config': TENANT_PORTAL_CONFIG,
            '/v1/approvals': {'count': 0, 'items': []},
            path: task,
            path + '/events': [{'seq': 1, 'created_at': ago(48), 'type': 'agent.turn_started',
                                'payload': {'turn': 1, 'subtask': 'e', 'interjectible': True}}],
            path + '/dag': {'nodes': [], 'edges': []},
            path + '/conversation': {'items': items, 'cursor': str(len(items)), 'has_more': False},
            path + '/live': live,
        }.get(request.path, []))

    with portal_browser() as browser:
        page = browser.new_page(viewport={'width': width, 'height': height})
        page.set_default_timeout(5000)
        page.add_init_script("localStorage.setItem('sca-portal-token', 'fixture-member')")
        page.route('**/*', handle)
        page.goto('http://portal.test/portal#/tasks/task-live-fixture')
        chat = page.locator('#task-conversation')
        strip = chat.get_by_role('region', name='实时运行状态')
        strip.get_by_text('正常运行', exact=True).wait_for()
        strip.get_by_text('Agent 1 · DeepSeek Harness · e', exact=True).wait_for()
        strip.get_by_text('正在调用模型', exact=True).wait_for()
        assert strip.get_by_text('正在等待依赖', exact=False).is_visible()
        clock = strip.locator('.conversation-live-heading strong')
        first = clock.inner_text()
        page.wait_for_function('(first) => document.querySelector(".conversation-live-heading strong").textContent !== first',
                               arg=first, polling=50)
        screenshot(page, chat, tmp_path / f'task-{width}-running.png')
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        feed = chat.locator('.conversation-feed')
        pinned_y = strip.bounding_box()['y']
        feed.evaluate('(node) => node.scrollTop = 0')
        assert strip.bounding_box()['y'] == pinned_y

        live.update(last_activity_at=ago(135), stalled=True)
        lane.update(activity={'operation': 'command', 'detail': 'python -m pytest -q', 'started_at': ago(135)},
                    stalled=True, last_activity_at=ago(135))
        strip.get_by_text('可能卡住', exact=True).wait_for()
        assert strip.locator('.conversation-live-health').get_by_text(re.compile(r'已 2 分 \d+ 秒 无进展')).is_visible()
        assert strip.get_by_text('python -m pytest -q', exact=True).is_visible()
        assert strip.evaluate('(node) => node.scrollWidth === node.clientWidth')
        assert strip.get_by_role('link', name='查看日志').get_attribute('href') == '#timeline'
        action = strip.get_by_role('button', name='打断并发送…')
        action.evaluate('(button) => button.click()')
        assert chat.get_by_role('textbox', name='插话').evaluate('(node) => node === document.activeElement')
        screenshot(page, chat, tmp_path / f'task-{width}-stalled.png')

        operation = feed.locator('.conversation-activity')
        operation.evaluate('(node) => node.open = true')
        assert operation.get_by_text('用时 4.1 秒', exact=True).is_visible()
        assert operation.get_by_text('退出码 1', exact=True).is_visible()
        tail = feed.locator('.conversation-step-output')
        assert tail.get_by_text('AssertionError: expected 2, got 1').is_hidden()
        tail.evaluate('(node) => node.open = true')
        assert tail.get_by_text('AssertionError: expected 2, got 1').is_visible()
        tail.evaluate('(node) => node.open = false')
        operation.evaluate('(node) => node.open = false')

        task['state'] = 'SUCCEEDED'
        live.clear()
        live.update(snapshot('SUCCEEDED', 'finished', finished=True), summary={
            'phases': [{'phase': 'setup', 'duration_s': 5.2}, {'phase': 'plan', 'duration_s': 31},
                       {'phase': 'subtasks', 'duration_s': 65}, {'phase': 'verification', 'duration_s': 4.1}],
            'agents': [{**lane, 'duration_s': 39}],
        })
        summary = chat.locator('.conversation-run-summary')
        summary.get_by_text('各阶段用时', exact=True).wait_for()
        assert summary.get_by_text('总费用 $0.003', exact=True).is_visible()
        assert summary.get_by_text('39 秒 · $0.003', exact=True).is_visible()
        assert strip.get_by_text('总用时 3 分 30 秒', exact=True).is_visible()
        assert strip.get_by_text('可能卡住', exact=True).is_hidden()
        feed.evaluate('(node) => node.scrollTop = node.scrollHeight')
        screenshot(page, chat, tmp_path / f'task-{width}-finished.png')
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        assert 'SUCCEEDED' in polls and polls.count('RUNNING') >= 2


@pytest.mark.parametrize(('width', 'height'), [(1440, 1000), (390, 844)])
def test_portal_browser_live_evolution_case_and_budget(tmp_path, width, height):
    path = '/v1/evolutions/evolution-live-fixture'
    row = {'id': 'evolution-live-fixture', 'status': 'running', 'spent_usd': .02, 'created_at': ago(210),
           'config': {'suite': 'dsh-smoke', 'agent_backend': 'dsh', 'model': 'fake', 'budget_usd': .25},
           'candidates': [], 'journal': []}
    live = snapshot('running', 'evaluation')
    live.update(generation=2, generation_started_at=ago(120), candidate=1, budget_usd=.25, eval_spent_usd=.02,
                cost_usd=.023, evaluation={
                    'split': 'dev', 'completed': 3, 'total': 10, 'passed': 2,
                    'cases': [{'task_id': 'task-case', 'label': 'empty-input · 第 1 次', 'state': 'RUNNING',
                               'started_at': ago(69), 'stalled': False, 'last_activity_at': ago(8)}],
                })

    def handle(route):
        request = urlsplit(route.request.url)
        if serve_portal(route, request.path):
            return
        route.fulfill(json={
            '/v1/portal/whoami': {'role': 'admin', 'tenant_id': 'eval'},
            '/v1/portal/config': TENANT_PORTAL_CONFIG,
            '/v1/approvals': {'count': 0, 'items': []},
            path: row, path + '/dag': {'nodes': [], 'edges': []},
            path + '/conversation': {'items': [], 'cursor': 'e:0', 'has_more': False},
            path + '/live': live,
        }.get(request.path, []))

    with portal_browser() as browser:
        page = browser.new_page(viewport={'width': width, 'height': height})
        page.set_default_timeout(5000)
        page.add_init_script("localStorage.setItem('sca-portal-token', 'fixture-admin')")
        page.route('**/*', handle)
        page.goto('http://portal.test/portal#/evolutions/runs/evolution-live-fixture')
        chat = page.locator('#evolution-conversation')
        strip = chat.get_by_role('region', name='实时运行状态')
        strip.get_by_text('第 2 代 · 候选 1', exact=True).wait_for()
        assert strip.get_by_text('已完成 3/10 用例 · 通过 2', exact=True).is_visible()
        assert strip.get_by_text('费用 $0.023 · 评测 $0.02 / $0.25', exact=True).is_visible()
        case = strip.get_by_role('link', name='empty-input · 第 1 次', exact=True)
        assert case.is_visible() and case.get_attribute('href') == '#/tasks/task-case'
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        screenshot(page, chat, tmp_path / f'evolution-{width}-running.png')

        live.update(stalled=True, last_activity_at=ago(135))
        live['evaluation']['cases'][0].update(stalled=True, last_activity_at=ago(135))
        strip.get_by_text('可能卡住', exact=True).wait_for()
        assert strip.get_by_role('link', name='查看日志').get_attribute('href') == '#/tasks/task-case'
        strip.get_by_role('button', name='立即生效…').evaluate('(node) => node.click()')
        assert chat.get_by_role('textbox', name='给提议的指引').evaluate('(node) => node === document.activeElement')
        screenshot(page, chat, tmp_path / f'evolution-{width}-stalled.png')

        row['status'] = 'completed'
        live.update(snapshot('completed', 'finished', finished=True), cost_usd=.023, summary={
            'phases': [{'phase': 'evaluation_dev', 'duration_s': 82},
                       {'phase': 'evaluation_holdout', 'duration_s': 51}],
            'agents': [{'label': '评测 Agent', 'backend': 'dsh', 'duration_s': 95, 'cost_usd': .02}],
        })
        summary = chat.locator('.conversation-run-summary')
        summary.get_by_text('开发集评测', exact=True).wait_for()
        assert summary.get_by_text('总费用 $0.023', exact=True).is_visible()
        assert chat.get_by_text('等待运行进度…', exact=True).is_hidden()
        assert strip.get_by_text('总用时 3 分 30 秒', exact=True).is_visible()
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        screenshot(page, chat, tmp_path / f'evolution-{width}-finished.png')
