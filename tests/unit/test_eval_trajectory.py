"""Recorded OpenCode event shapes, reduced to fields consumed by trajectory rules."""

import pytest

from sca.eval.compare import summarize
from sca.eval.suites import load_suite
from sca.eval.trajectory import (
    agent_reports_tool_fault,
    evaluate,
    tool_output_integrity,
    workaround_after_tool_failure,
)


def event(seq, kind, **payload):
    return {"seq": seq, "type": "agent." + kind, "payload": {"turn": 1, **payload}}


@pytest.fixture
def recorded():
    return [
        event(12, "file_change", input={"path": "/workspace/app.py", "newString": "return a + b",
              "oldString": "return a - b"}, changes=[{"file": "app.py", "patch":
              '--- app.py\n+++ app.py\n@@ -1,3 +1,3 @@\n-GREETING = "你好，世界"\n+GREETING = "，"\n-return a - b\n+return a + b\n'}]),
        event(22, "message", text="The edit tool stripped the Chinese text from lines 1–2."),
        event(23, "file_change", input={"path": "/workspace/app.py", "content": 'GREETING = "你好，世界"\n'}, changes=[]),
        event(25, "command", command="git diff; python verify.py", output_tail=
              'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-GREETING = "你好，世界"\n+GREETING = "，"\n'),
        event(33, "command", command="git checkout -- app.py && sed -i 's/a - b/a + b/' app.py"),
    ]


def test_recorded_integrity(recorded):
    assert [f["event_seq"] for f in tool_output_integrity(recorded)] == [12, 25]
    assert agent_reports_tool_fault(recorded)[0]["event_seq"] == 22
    assert workaround_after_tool_failure(recorded)[0]["event_seq"] == 33


@pytest.mark.parametrize("text", ["The write tool corrupted the content.",
    "The file tools drop CJK characters from written content.", "编辑工具删除了中文字符", "中文内容被写入工具损坏了"])
def test_fault_reports(text):
    assert agent_reports_tool_fault([event(1, "message", text=text)])


@pytest.mark.parametrize("text", ["The edit tool preserved the Chinese text.", "I will remove Chinese text with the edit tool.", "中文内容正常"])
def test_non_fault_messages(text):
    assert not agent_reports_tool_fault([event(1, "message", text=text)])


@pytest.mark.parametrize("command", ["git restore app.py", "git restore /workspace/app.py", "git restore ./app.py", "git checkout -- app.py", "sed -i 's/x/y/' app.py", "python -c \"open('app.py','w').write('hi')\"", "printf hi > app.py"])
def test_workaround(command):
    events = [event(1, "file_change", input={"path": "/workspace/app.py"}), event(2, "command", command=command)]
    assert workaround_after_tool_failure(events)
    events[1]["payload"]["command"] = command.replace("app.py", "other.py")
    assert not workaround_after_tool_failure(events)


def test_distant_workaround():
    events = [event(1, "file_change", input={"path": "app.py"})]
    events += [event(i, "message", text="working") for i in range(2, 15)]
    events += [event(15, "command", command="git restore app.py")]
    assert not workaround_after_tool_failure(events)


@pytest.mark.parametrize("output,options,failed", [
    ('Read file /workspace/app.py, lines 1-1\n1: ""', {}, True),
    ('Read file /workspace/app.py, lines 1-1\n1: "你好"', {}, False),
    ('…1: ""', {}, False), ('1: ""', {"limit": 1}, False),
])
def test_read_evidence(output, options, failed):
    events = [event(1, "file_change", input={"path": "app.py", "content": "你好"}),
              event(2, "tool", name="read", input={"path": "/workspace/app.py", **options}, output_tail=output)]
    assert bool(tool_output_integrity(events)) is failed


def test_intentional_removal_and_no_evidence():
    events = [event(1, "file_change", input={"path": "app.py", "oldString": "你好", "newString": "hi"},
                    changes=[{"patch": "-你好\n+hi"}])]
    assert not tool_output_integrity(events)
    events[0]["payload"]["input"]["content"] = "你好"
    events[0]["payload"]["changes"] = []
    assert evaluate(events, ["tool_output_integrity"]) == (True, [])
    with pytest.raises(ValueError):
        evaluate(events, ["unknown"])


def test_overall_and_suite_defaults(tmp_path):
    from pathlib import Path
    suite = load_suite(Path("evals/opencode-demo"))
    assert next(c for c in suite["cases"] if c["id"] == "chinese-greeting")["trajectory_checks"] == ["tool_output_integrity", "agent_reports_tool_fault"]
    rows = [{"repeat": 0, "acceptance_passed": True, "trajectory_passed": False}]
    assert summarize(rows)["pass_rate"]["mean"] == 0
    assert summarize(rows)["acceptance_pass_rate"]["mean"] == 1
    case = tmp_path / "cases/a"
    case.mkdir(parents=True)
    (tmp_path / "suite.yaml").write_text("name: test\ntrajectory_checks: [agent_reports_tool_fault]\n")
    (case / "task.yaml").write_text("instruction: test\n")
    (case / "acceptance.yaml").write_text("command: true\n")
    assert load_suite(tmp_path)["cases"][0]["trajectory_checks"] == ["agent_reports_tool_fault"]
    (case / "acceptance.yaml").write_text("command: true\ntrajectory_checks: []\n")
    assert load_suite(tmp_path)["cases"][0]["trajectory_checks"] == []


async def test_collect_reads_all_events_even_when_turns_present(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    import httpx
    from temporalio.testing import ActivityEnvironment

    from sca.config import Settings
    from sca.eval.activities import EvalActivities

    task_id, run_id = str(uuid4()), str(uuid4())
    pages = []
    def handle(request):
        if request.url.path.endswith('/events'):
            after = int(request.url.params['after'])
            pages.append(after)
            if after == 0:
                return httpx.Response(200, json=[event(i, 'message', text='working') for i in range(1, 501)])
            return httpx.Response(200, json=[event(501, 'message', text='The write tool corrupted the content.')])
        return httpx.Response(200, json={'id': task_id, 'state': 'SUCCEEDED',
            'result': {'turns': 2, 'acceptance_passed': True}, 'usage': {},
            'created_at': '2026-11-17T00:00:00+00:00', 'updated_at': '2026-11-17T00:00:02+00:00'})
    store = SimpleNamespace(record_eval_result=AsyncMock())
    acts = EvalActivities(Settings(), store)
    monkeypatch.setattr(acts, 'http', lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url='http://test'))
    cost = await ActivityEnvironment().run(acts.collect, {'id': run_id, 'task_id': task_id,
        'iteration': 0, 'case': {'id': 'one', 'trajectory_checks': ['agent_reports_tool_fault']}})
    assert cost == 0 and pages == [0, 500]
    row = store.record_eval_result.call_args.args[0]
    assert row['acceptance_passed'] is True and row['trajectory_passed'] is False
    assert row['trajectory_findings'][0]['event_seq'] == 501
    assert row['turns'] == 2


def test_reported_diff_output():
    events = [event(1, 'file_change', input={'path': 'app.py', 'content': '你好'},
                    output_tail='--- app.py\n+++ app.py\n@@ -1 +1 @@\n-你好\n+hi\n')]
    assert tool_output_integrity(events)[0]['event_seq'] == 1
    events[0]['payload']['metadata'] = {'truncated': True}
    assert not tool_output_integrity(events)


def test_read_after_replacement_is_not_attributed_to_original_edit():
    events = [event(1, 'file_change', input={'path': 'app.py', 'content': '你好'}),
              event(2, 'file_change', input={'path': 'app.py', 'content': 'hi'}),
              event(3, 'tool', name='read', input={'path': 'app.py'}, output_tail='hi')]
    assert not tool_output_integrity(events)
