import json
import runpy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest


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
    health = json.loads(responses["/healthz"][1])
    assert health["portal"] == "archive"
    assert health["task_progress"] is True


def test_task_progress_and_memory_use_the_published_events():
    module = runpy.run_path("scripts/serve_portal_history.py")
    responses = module["archive_responses"]()

    def read(path: str):
        return json.loads(responses[path][1])

    archive = read("/portal/archive.json")
    tasks = read("/v1/tasks")
    assert [task["id"] for task in tasks] == [task["id"] for task in archive["tasks"]]
    for task in archive["tasks"]:
        path = "/v1/tasks/" + task["id"]
        assert read(path)["archive_elapsed_ms"] == task["elapsedMs"]
        assert len(read(path + "/events")) == task["eventCount"]

    (memory,) = read("/v1/memories")
    path = f'/v1/tasks/{memory["source_task_id"]}/events'
    sequence = memory["source_event_seq"]
    filter_records = module["filter_archive_records"]
    _, body = filter_records(path, responses[path][1], {"after": [str(sequence - 1)]})
    first = json.loads(body)[0]
    assert first["seq"] == sequence
    assert first["type"] == "memory.injected"
    assert first["archive_offset_ms"] > 0
    assert "created_at" not in first
    assert first["payload"]["count"] == 1
    for status, scope, expected in [
        ("active", "订单", [memory]), ("active", "其他", []), ("retired", "订单", []),
    ]:
        _, body = filter_records("/v1/memories", responses["/v1/memories"][1], {
            "status": [status], "scope": [scope],
        })
        assert json.loads(body) == expected


@pytest.fixture(scope="module")
def archive_handler():
    return runpy.run_path("scripts/serve_portal_history.py")["HistoryHandler"]


@pytest.mark.parametrize(("method", "headers", "status"), [
    ("GET", "Host: localhost:18001\r\nOrigin: http://localhost:18001\r\n", 200),
    ("GET", "Host: 127.0.0.1:18001\r\nSec-Fetch-Site: same-origin\r\n", 200),
    ("HEAD", "Host: localhost:18001\r\n", 200),
    ("GET", "", 403),
    ("GET", "Host: archive.example:8001\r\n", 403),
    ("HEAD", "Host: archive.example:8001\r\n", 403),
    ("GET", "Host: localhost:8001\r\nhost: localhost:8001\r\n", 403),
    ("GET", "Host: localhost:8001\r\nX-Forwarded-For: 127.0.0.1\r\n", 403),
    ("GET", "Host: localhost:8001\r\nSec-Fetch-Site: cross-site\r\n", 403),
    ("GET", "Host: localhost:8001\r\nSec-Fetch-Site: same-site\r\n", 403),
    ("GET", "Host: localhost:8001\r\nOrigin: http://archive.example\r\n", 403),
])
def test_archive_guest_headers(archive_handler, method, headers, status):
    handler = archive_handler.__new__(archive_handler)
    handler.client_address = ("127.0.0.1", 50123)
    handler.server = SimpleNamespace(responses={"/v1/tasks": ("application/json", b'[{"id":"archive-task"}]')})
    handler.rfile = BytesIO(f"{method} /v1/tasks HTTP/1.1\r\n{headers}\r\n".encode())
    handler.wfile = BytesIO()

    handler.handle()

    head, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
    assert int(head.split()[1]) == status
    assert (b"archive-task" in body) is (status == 200 and method == "GET")
