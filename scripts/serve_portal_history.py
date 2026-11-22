"""Serve the Portal and its published archive on a local, read-only HTTP endpoint."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "sca" / "api" / "static"
HISTORY = ROOT / "Milestone" / "history"
HTML_TYPE = "text/html; charset=utf-8"
SCRIPT_TYPE = "text/javascript; charset=utf-8"
JSON_TYPE = "application/json"
ARCHIVE_MARKER = b"<!-- archive bootstrap -->"
HISTORY_DATA = b'<script id="history-data" type="application/json">'
PORTAL_ASSETS = (
    "portal-operations.css",
    "task-dag.css",
    "task-dag.js",
    "portal-ui.js",
    "portal-markdown.js",
    "portal-tasks.js",
    "portal-evolution.js",
    "portal-settings.js",
    "portal-projects.js",
    "portal-archive.js",
)


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
    tasks = [
        {
            "id": task["id"],
            "kind": task["kind"],
            "date": task["date"],
            "state": task["state"],
            "elapsedMs": task["elapsedMs"],
            "eventCount": len(task["events"]),
        }
        for task in json.loads(raw)["tasks"]
    ]
    archive = json.loads((HISTORY / "evaluation-history.json").read_bytes())
    archive["tasks"] = tasks
    responses = {
        "/": (HTML_TYPE, portal),
        "/portal": (HTML_TYPE, portal),
        "/portal/history": (HTML_TYPE, history),
        "/portal/architecture": (HTML_TYPE, (ROOT / "Milestone" / "index.html").read_bytes()),
        "/portal/archive.json": (JSON_TYPE, json.dumps(archive, ensure_ascii=False).encode()),
        "/healthz": (JSON_TYPE, b'{"status":"ok","mode":"history","portal":"archive"}\n'),
    }
    for name in ("task-flow.js", "task-timeline.js"):
        responses[f"/portal/{name}"] = (SCRIPT_TYPE, (STATIC / name).read_bytes())
    for name in PORTAL_ASSETS:
        media_type = "text/css; charset=utf-8" if name.endswith(".css") else SCRIPT_TYPE
        responses[f"/portal/assets/{name}"] = (media_type, (STATIC / name).read_bytes())
    return responses


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
        path = self.path.split("?", 1)[0]
        page = self.server.responses.get(path)
        if page is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        media_type, body = page
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
