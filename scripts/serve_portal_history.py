"""Serve the Portal and its published archive on a local, read-only HTTP endpoint."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    # The archive viewer runs from a source checkout before SCA is installed.
    sys.path.insert(0, str(ROOT / "src"))

from sca.api.local_request import local_guest_request

HOST = "127.0.0.1"
STATIC = ROOT / "src" / "sca" / "api" / "static"
HISTORY = ROOT / "Milestone" / "history"
HTML_TYPE = "text/html; charset=utf-8"
SCRIPT_TYPE = "text/javascript; charset=utf-8"
JSON_TYPE = "application/json"
ARCHIVE_MARKER = b"<!-- archive bootstrap -->"
HISTORY_DATA = b'<script id="history-data" type="application/json">'
PORTAL_ASSETS = (
    "portal-operations.css",
    "portal-conversation.css",
    "task-dag.css",
    "task-dag.js",
    "portal-ui.js",
    "portal-conversation.js",
    "portal-models.js",
    "portal-markdown.js",
    "portal-tasks.js",
    "portal-evolution.js",
    "portal-settings.js",
    "portal-projects.js",
    "portal-archive.js",
)


def json_page(value: object) -> tuple[str, bytes]:
    return JSON_TYPE, json.dumps(value, ensure_ascii=False).encode()


def task_details(task: dict) -> dict:
    return {
        "id": task["id"],
        "kind": task["kind"],
        "state": task["state"],
        "created_at": task["date"],
        "archive_elapsed_ms": task["elapsedMs"],
        "spec": {"agent_backend": task["backend"]},
        "result": {
            "verification_passed": task["verification"],
            "acceptance_passed": task["acceptance"],
        },
    }


def task_event(sequence: int, kind: str, offset: int, payload: dict) -> dict:
    return {"seq": sequence, "type": kind, "archive_offset_ms": offset, "payload": payload}


def archive_responses() -> dict[str, tuple[str, bytes]]:
    portal = (STATIC / "portal.html").read_bytes()
    if portal.count(ARCHIVE_MARKER) != 1:
        raise ValueError("Missing Portal archive bootstrap marker")
    portal = portal.replace(
        ARCHIVE_MARKER, b'<script src="/portal/assets/portal-archive.js"></script>'
    )
    history = (HISTORY / "task-history.html").read_bytes()
    _, marker, tail = history.partition(HISTORY_DATA)
    raw, end, _ = tail.partition(b"</script>")
    if not marker or not end:
        raise ValueError("Missing published task history data")
    published_tasks = json.loads(raw)["tasks"]
    tasks = [
        {
            "id": task["id"],
            "kind": task["kind"],
            "backend": task["backend"],
            "date": task["date"],
            "state": task["state"],
            "elapsedMs": task["elapsedMs"],
            "eventCount": len(task["events"]),
        }
        for task in published_tasks
    ]
    archive = json.loads((HISTORY / "evaluation-history.json").read_bytes())
    archive["tasks"] = tasks
    archive["eventCounts"] = Counter(kind for task in published_tasks for _, kind, _, _ in task["events"])
    archive["memoryHits"] = sum(
        payload.get("count", 0) > 0
        for task in published_tasks
        for _, kind, _, payload in task["events"]
        if kind == "memory.injected"
    )
    details = [task_details(task) for task in published_tasks]
    responses = {
        "/": (HTML_TYPE, portal),
        "/portal": (HTML_TYPE, portal),
        "/portal/history": (HTML_TYPE, history),
        "/portal/architecture": (HTML_TYPE, (ROOT / "Milestone" / "index.html").read_bytes()),
        "/portal/archive.json": json_page(archive),
        "/v1/tasks": json_page(details),
        "/v1/memories": json_page(archive["memories"]),
        "/healthz": (JSON_TYPE, b'{"status":"ok","mode":"history","portal":"archive","task_progress":true}\n'),
    }
    for record, source in zip(details, published_tasks):
        path = f'/v1/tasks/{record["id"]}'
        responses[path] = json_page(record)
        responses[f"{path}/events"] = json_page([task_event(*event) for event in source["events"]])
        responses[f"{path}/artifacts"] = json_page([])
    for name in ("task-flow.js", "task-timeline.js"):
        responses[f"/portal/{name}"] = (SCRIPT_TYPE, (STATIC / name).read_bytes())
    for name in PORTAL_ASSETS:
        media_type = "text/css; charset=utf-8" if name.endswith(".css") else SCRIPT_TYPE
        responses[f"/portal/assets/{name}"] = (media_type, (STATIC / name).read_bytes())
    return responses


def filter_archive_records(path: str, body: bytes, query: dict[str, list[str]]) -> tuple[str, bytes]:
    if path == "/v1/tasks":
        limit = int(query.get("limit", ["100"])[0])
        if limit < 1:
            raise ValueError("limit must be positive")
        return json_page(json.loads(body)[:limit])
    if path.endswith("/events"):
        after = int(query.get("after", ["0"])[0])
        return json_page([event for event in json.loads(body) if event["seq"] > after][:500])
    if path == "/v1/memories":
        status = query.get("status", [""])[0]
        scope = query.get("scope", [""])[0].casefold()
        return json_page([
            record for record in json.loads(body)
            if (not status or record["status"] == status) and scope in record["scope"].casefold()
        ])
    return JSON_TYPE, body


class HistoryServer(ThreadingHTTPServer):
    def __init__(self, port: int, responses: dict[str, tuple[str, bytes]]) -> None:
        self.responses = responses
        super().__init__((HOST, port), HistoryHandler)


class HistoryHandler(BaseHTTPRequestHandler):
    server: HistoryServer

    def do_GET(self) -> None:
        self.serve_page(send_body=True)

    def do_HEAD(self) -> None:
        self.serve_page(send_body=False)

    def serve_page(self, *, send_body: bool) -> None:
        if not local_guest_request(self.client_address[0], self.headers.items()):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        path = target.path
        page = self.server.responses.get(path)
        if page is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        media_type, body = page
        if path.startswith("/v1/"):
            try:
                media_type, body = filter_archive_records(path, body, parse_qs(target.query))
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if send_body:
            self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, help="local port (default: 8000)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    try:
        responses = archive_responses()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Could not load the published Portal archive: {exc}\n")

    try:
        server = HistoryServer(args.port, responses)
    except OSError as exc:
        parser.exit(1, f"Could not start the history server on {HOST}:{args.port}: {exc}\n")

    with server:
        print(f"Archive Portal: http://{HOST}:{server.server_port}/portal", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
