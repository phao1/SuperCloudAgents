"""Serve the published task history on a local, read-only HTTP endpoint."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
HISTORY_FILE = Path(__file__).resolve().parents[1] / "Milestone" / "history" / "task-history.html"


class HistoryServer(ThreadingHTTPServer):
    def __init__(self, port: int, document: bytes) -> None:
        self.document = document
        super().__init__((HOST, port), HistoryHandler)


class HistoryHandler(BaseHTTPRequestHandler):
    server: HistoryServer

    def do_GET(self) -> None:
        self.serve_page(send_body=True)

    def do_HEAD(self) -> None:
        self.serve_page(send_body=False)

    def serve_page(self, *, send_body: bool) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/portal"}:
            body = self.server.document
            content_type = "text/html; charset=utf-8"
        elif path == "/healthz":
            body = b'{"status":"ok","mode":"history"}\n'
            content_type = "application/json"
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
        document = HISTORY_FILE.read_bytes()
    except OSError as exc:
        parser.exit(1, f"Could not read the published history file: {exc}\n")

    try:
        server = HistoryServer(args.port, document)
    except OSError as exc:
        parser.exit(1, f"Could not start the history server on {HOST}:{args.port}: {exc}\n")

    with server:
        print(f"History Portal: http://{HOST}:{server.server_port}/portal", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
