"""Coverage evidence must be conservative and safe to share."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("coverage_report", ROOT / "scripts/coverage_report.py")
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


def test_map_parity():
    entries = json.loads((ROOT / "Milestone/coverage-map.json").read_text())["modules"]
    modules = json.loads((ROOT / "Milestone/data.json").read_text())["modules"]
    assert set(entries) == {m["id"] for m in modules}
    assert len(entries) == 64
    assert all(not e["evidence"] for e in entries.values() if e["platform"] == "planned")


def test_predicates():
    rule = {"event": "memory.injected", "payload": {"ids": {"len_gt": 0}, "method": "vector"}}
    assert coverage.matches(rule, {"type": "memory.injected", "payload": {"ids": ["a"], "method": "vector"}})
    assert not coverage.matches(rule, {"type": "memory.injected", "payload": {"ids": [], "method": "vector"}})
    assert not coverage.matches(rule, {"type": "task.created"})
    assert not coverage.matches({"event": "x", "payload": {"n": {"gt": 0}}}, {"type": "x", "payload": {"n": True}})
    assert coverage.matches({"event": "x", "payload": {"n": {"gt": 0}}}, {"type": "x", "payload": {"n": 1}})
    assert not coverage.matches({"event": "x", "payload": {"a": {"nonempty": True}}}, {"type": "x"})


def test_offline_record_and_no_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCA_EVAL_API_TOKEN", "secret-sentinel-never-output")
    record = ROOT / "Milestone/records/2026-11-19-order-events.jsonl"
    output = tmp_path / "report.json"
    assert coverage.main(["--jsonl", str(record), "--json", "--out", str(output)]) == 0
    data = json.loads(output.read_text())
    rows = {r["id"]: r for r in data["modules"]}
    assert rows["admission_control"]["evidence"] == "de960065 #2 admission.granted"
    assert rows["tool_domain"]["status"] == "planned"
    assert rows["gateway_limiter"]["status"] == "manual-only"
    assert rows["evo_eval"]["status"] != "covered"
    assert len(rows) == 64
    assert coverage.main(["--jsonl", str(record)]) == 0
    captured = capsys.readouterr()
    assert "covered " in captured.out
    assert "secret-sentinel" not in captured.out + captured.err + output.read_text()


def test_token_file_only_selected_line(tmp_path, monkeypatch):
    monkeypatch.delenv("SCA_EVAL_API_TOKEN", raising=False)
    path = tmp_path / "portal.env"
    path.write_text("OTHER=ignore\nSCA_EVAL_API_TOKEN=\"test-token\"\nOTHER=ignore\n")
    assert coverage.read_token(path) == "test-token"


def test_event_paging():
    client = coverage.Client("http://localhost:8000", "test")
    pages = [[{"seq": n} for n in range(1, 501)], [{"seq": 501}]]
    paths = []

    def get(path):
        paths.append(path)
        return pages.pop(0)

    client.get = get
    assert len(client.events("task")) == 501
    assert "after=500" in paths[1]


def test_eval_run_includes_tasks(monkeypatch, capsys):
    paths = []

    def get(self, path):
        paths.append(path)
        if path == "/evals/runs/run":
            return {"id": "run", "results": [{"task_id": "task", "acceptance_passed": False}]}
        if path == "/tasks/task":
            return {"id": "task"}
        return [{"task_id": "task", "seq": 1, "type": "task.created"}]

    monkeypatch.setattr(coverage.Client, "get", get)
    assert coverage.main(["--runs", "run", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "/tasks/task/events?after=0&limit=500" in paths
    assert next(r for r in data["modules"] if r["id"] == "evo_eval")["status"] == "covered"


def test_capped_since_fails_without_leaking_token(monkeypatch, capsys):
    monkeypatch.setenv("SCA_EVAL_API_TOKEN", "secret-sentinel")
    monkeypatch.setattr(coverage.Client, "get", lambda self, path: [{}] * 500)
    assert coverage.main(["--since", "2026-11-01T00:00:00Z"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "secret-sentinel" not in captured.err


def test_api_exception_is_redacted(monkeypatch, capsys):
    def get(self, path):
        raise ValueError("secret-sentinel")

    monkeypatch.setattr(coverage.Client, "get", get)
    assert coverage.main(["--tasks", "task"]) == 1
    assert "secret-sentinel" not in str(capsys.readouterr())


def test_queued_eval_run_is_not_execution_evidence():
    data = coverage.report([], [{"id": "queued", "results": []}])
    assert next(r for r in data["modules"] if r["id"] == "evo_eval")["status"] == "not covered"
