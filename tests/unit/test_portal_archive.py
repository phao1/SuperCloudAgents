import json
import runpy
from pathlib import Path


def test_windows_archive_uses_portal_shell_and_published_records():
    module = runpy.run_path("scripts/serve_portal_history.py")
    responses = module["archive_responses"]()
    portal = responses["/portal"][1].decode("utf-8")

    assert '<script src="/portal/assets/portal-archive.js"></script>' in portal
    assert 'href="#/overview"' in portal
    assert 'href="#/evals"' in portal
    assert responses["/portal/assets/portal-archive.js"][1]
    assert responses["/portal/history"][1] == (
        Path("Milestone/history/task-history.html").read_bytes()
    )

    archive = json.loads(responses["/portal/archive.json"][1])
    assert len(archive["tasks"]) == 64
    assert sum(task["eventCount"] for task in archive["tasks"]) == 2869
    assert len(archive["runs"]) == 3
    assert json.loads(responses["/healthz"][1])["portal"] == "archive"
