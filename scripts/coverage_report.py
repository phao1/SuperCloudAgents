"""Report observed architecture execution; uses only the Python standard library."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


def matches(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    if rule.get("event") != event.get("type"):
        return False
    for key, expected in rule.get("payload", {}).items():
        payload = event.get("payload", {})
        if key not in payload:
            return False
        value = payload[key]
        if isinstance(expected, dict):
            if "gt" in expected:
                if type(value) not in (int, float) or value <= expected["gt"]:
                    return False
            elif "len_gt" in expected:
                if not isinstance(value, (list, str)) or len(value) <= expected["len_gt"]:
                    return False
            elif expected == {"nonempty": True}:
                if not value:
                    return False
            else:
                return False
        elif type(value) is not type(expected) or value != expected:
            return False
    return True


def read_token(path: Path = ROOT / ".data/portal.env") -> str:
    token = os.environ.get("SCA_EVAL_API_TOKEN", "")
    if token:
        return token
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("SCA_EVAL_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("\"\x27")
    return ""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API redirects are not allowed")


class Client:
    def __init__(self, api: str, token: str):
        parts = urlsplit(api)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username:
            raise ValueError("Invalid API URL")
        self.api = api.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def get(self, path: str) -> Any:
        request = Request(self.api + "/v1" + path,
                          headers={"Authorization": "Bearer " + self.token})
        with self.opener.open(request, timeout=30) as response:
            return json.load(response)

    def events(self, task_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after = 0
        while True:
            page = self.get(f"/tasks/{task_id}/events?after={after}&limit=500")
            if not page:
                return rows
            next_after = max(row["seq"] for row in page)
            if next_after <= after:
                raise ValueError("Event pagination did not advance")
            rows.extend(page)
            after = next_after
            if len(page) < 500:
                return rows


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("ISO timestamps must include a timezone")
    return result


def report(events: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    mapping = json.loads((ROOT / "Milestone/coverage-map.json").read_text())["modules"]
    modules = json.loads((ROOT / "Milestone/data.json").read_text())["modules"]
    rows = []
    for module in modules:
        entry = mapping[module["id"]]
        evidence = None
        for event in sorted(events, key=lambda e: (e.get("at", e.get("created_at", "")),
                                                  e.get("task_id", ""), e["seq"])):
            if any(matches(rule, event) for rule in entry["evidence"] if "event" in rule):
                evidence = f"{event['task_id'][:8]} #{event['seq']} {event['type']}"
                break
        completed_runs = [run for run in runs if any(
            type(row.get("acceptance_passed")) is bool for row in run.get("results", []))]
        if evidence is None and completed_runs and {"eval_run": True} in entry["evidence"]:
            evidence = "eval run " + str(completed_runs[0]["id"])[:8]
        status = ("planned" if entry["platform"] == "planned" else "covered" if evidence
                  else "not covered" if entry["evidence"] else "manual-only")
        rows.append({"id": module["id"], "name": module["name"], "status": status,
                     "evidence": evidence, "how": entry["how"] if not evidence else "",
                     "manual": entry["manual"], "platform": entry["platform"]})
    covered = sum(row["status"] == "covered" for row in rows)
    manual = sum(row["status"] == "manual-only" for row in rows)
    planned = sum(row["status"] == "planned" for row in rows)
    return {"summary": f"covered {covered} / {len(rows)} (manual {manual}, planned {planned})",
            "modules": rows}


def markdown(data: dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        return str(value or "—").replace("|", "\\|").replace("\n", " ")
    lines = [data["summary"], "", "事件证明模块被执行，不代表完整能力或正确性；manual 为仅能人工核对的模块数。", "",
             "| 模块 | 状态 | 首条证据 | 覆盖方式 / 人工检查 |", "|---|---|---|---|"]
    for row in data["modules"]:
        lines.append("| " + " | ".join(cell(value) for value in (
            row["id"], row["status"], row["evidence"],
            " ".join(dict.fromkeys(filter(None, [row["how"], row["manual"]]))))) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--tasks", nargs="+", default=[])
    parser.add_argument("--runs", nargs="+", default=[])
    parser.add_argument("--since")
    parser.add_argument("--jsonl", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        runs = []
        if args.jsonl:
            if args.runs:
                raise ValueError("Offline JSONL does not contain evaluation-run records")
            events = [json.loads(line) for line in args.jsonl.read_text().splitlines() if line.strip()]
            selected = set(args.tasks)
            if args.since:
                since = timestamp(args.since)
                recent = {e["task_id"] for e in events if e["type"] == "task.created"
                          and timestamp(e.get("at", e.get("created_at"))) >= since}
                selected.update(recent)
            if args.tasks or args.since:
                events = [e for e in events if e["task_id"] in selected]
        else:
            client = Client(args.api, read_token())
            selected = set(args.tasks)
            for run_id in args.runs:
                run = client.get("/evals/runs/" + run_id)
                runs.append(run)
                selected.update(row["task_id"] for row in run["results"] if row.get("task_id"))
            if args.since:
                since = timestamp(args.since)
                tasks = client.get("/tasks?limit=500")
                if len(tasks) >= 500:
                    raise ValueError("Task list reached 500: API has no paging; use explicit --tasks")
                selected.update(t["id"] for t in tasks if timestamp(t["created_at"]) >= since)
            events = []
            for task_id in sorted(selected):
                client.get("/tasks/" + task_id)
                events.extend(client.events(task_id))
            if not selected and not args.since and not args.runs:
                raise ValueError("Select --tasks, --runs, --since or --jsonl")
        data = report(events, runs)
        output = json.dumps(data, ensure_ascii=False, indent=2) + "\n" if args.json else markdown(data)
        if args.out:
            args.out.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, HTTPError, URLError):
        # Never echo response bodies, URLs, or exception text containing credentials.
        print("Coverage report failed: check inputs, API access and task-list limit (500).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
