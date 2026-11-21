#!/usr/bin/env python3
"""Submit and follow the execution graph demo on an existing SCA deployment."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from uuid import UUID

import httpx

GRAPH = {
    "currency": (),
    "fx": ("currency",),
    "tax": ("currency",),
    "pricing": ("fx", "tax"),
    "invoices": ("pricing",),
    "reports": ("invoices",),
    "migration": ("currency",),
    "acceptance": ("reports", "migration"),
}
FILES = {name: f"dag-demo/{name}.txt" for name in GRAPH}
PORTAL = "/portal#/tasks/"


def _write(path: str, message: str) -> str:
    return f"printf '%s\\n' {shlex.quote(message)} > {shlex.quote(path)}"


def _check(names: tuple[str, ...]) -> str:
    return " && ".join(f"test -f {FILES[name]}" for name in names)


def _case(entries: list[tuple[str, str]], fallback: str) -> str:
    return "\n".join([
        'case "$(git branch --show-current)" in',
        *(f"  {pattern}) {commands} ;;" for pattern, commands in entries),
        f"  *) {fallback} ;;",
        "esac",
    ])


def submission(repo: str, base_ref: str = "main") -> dict:
    subtasks = [
        {
            "name": name, "instruction": f"Produce the fixture for {name}.",
            "paths": [f"dag-demo/{name}.*"], "depends_on": list(needs),
            **({"best_of": {"n": 3}} if name == "fx" else {}),
        }
        for name, needs in GRAPH.items()
    ]
    plan = {"split": True, "reason": "Independent work with shared prerequisites", "subtasks": subtasks}
    encoded = shlex.quote(json.dumps(plan, separators=(",", ":")))
    actions = [("*-plan-*", f"mkdir -p .sca && printf '%s' {encoded} > .sca/plan.json")]
    checks = []
    for name, needs in GRAPH.items():
        pattern = "*-best-of-[123]" if name == "fx" else f"*-{name}"
        destination = FILES[name] if name != "pricing" else "dag-demo/pricing.pending"
        prepare = f"{_check(needs)} && " if needs else ""
        write = f"{prepare}mkdir -p dag-demo && {_write(destination, name)}"
        if name == "fx":
            extra = _write("dag-demo/fx.notes", "alternative fixture")
            actions.append(("*-best-of-[13]", f"{write} && {extra}"))
        actions.append((pattern, write))
        checks.append((pattern, _check((*needs, name))))
    execute = _case(actions, _write("dag-demo/operator.txt", "operator guidance applied"))
    repair = _case(
        [("*-pricing", f"rm -f dag-demo/pricing.pending && {_write(FILES['pricing'], 'pricing')}")],
        "true",
    )
    verify = _case(checks, _check(tuple(GRAPH)))
    return {
        "kind": "coding",
        "spec": {
            "repo_url": repo,
            "base_ref": base_ref,
            "instruction": (
                "Demonstrate a multi-currency release using small fixture files.\n\n"
                f"```sca-fake\n{execute}\n```\n\n"
                f"```sca-fake\n{repair}\n```"
            ),
            "agent_backend": "fake",
            "plan": "auto",
            "verify_command": verify,
            "max_fix_attempts": 1,
            "turn_timeout_s": 60,
            "memory_enabled": False,
        },
    }


def _json(client: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def _wait(
    client: httpx.Client, path: str, deadline: float, interval: float,
    predicate: Callable[[dict], bool],
) -> dict:
    while time.monotonic() < deadline:
        task = _json(client, "GET", path)
        if predicate(task):
            return task
        if task["state"] in {"FAILED", "CANCELLED", "SUCCEEDED"}:
            raise RuntimeError(f"Demo task ended in state {task['state']} before the expected step")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise TimeoutError("Timed out waiting for a demo task step")


def _at_review(task: dict) -> bool:
    if task["state"] != "AWAITING_REVIEW":
        return False
    if task["result"].get("verification_passed") is not True:
        raise RuntimeError("Demo verification did not pass before review")
    return True


def run_demo(
    client: httpx.Client, repo: str, base_ref: str = "main", *,
    timeout_s: float = 600, interval_s: float = 1.0,
    on_submitted: Callable[[str], None] | None = None, approve: bool = True,
) -> str:
    initial = _json(client, "POST", "/v1/tasks", submission(repo, base_ref))
    task_id = str(UUID(initial["id"]))
    if on_submitted is not None:
        on_submitted(task_id)
    path = "/v1/tasks/" + task_id
    deadline = time.monotonic() + timeout_s
    _wait(client, path, deadline, interval_s, _at_review)
    message = _json(client, "POST", path + "/messages", {
        "text": "Record the operator acknowledgement before final approval.",
        "mode": "queue",
    })

    def ready_for_approval(task: dict) -> bool:
        if not _at_review(task):
            return False
        nodes = _json(client, "GET", path + "/dag")["nodes"]
        applied = next((node for node in nodes if
            node["kind"] == "interjection" and node["status"] == "succeeded"
            and node["meta"].get("message_id") == message["id"]
        ), None)
        if applied is None:
            return False
        turn = applied["meta"]["turn"]
        return any(
            node["kind"] == "review" and node["status"] == "running" and node["meta"].get("turn") == turn
            for node in nodes
        )

    _wait(client, path, deadline, interval_s, ready_for_approval)
    if approve:
        _json(client, "POST", path + "/review", {"decision": "approve"})
        _wait(client, path, deadline, interval_s, lambda task: task["state"] == "SUCCEEDED")
    return task_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("SCA_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--repo", required=True, help="Disposable Git repository visible to the worker")
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--hold-review", action="store_true", help="Leave final approval to the operator")
    args = parser.parse_args()
    token = os.environ.get("SCA_API_TOKEN")
    if not token:
        parser.error("SCA_API_TOKEN is required")
    if not (0 < args.timeout < float("inf")):
        parser.error("--timeout must be a positive finite number")
    try:
        with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {token}"},
                          timeout=15, follow_redirects=False) as client:
            run_demo(
                client, args.repo, args.base_ref, timeout_s=args.timeout,
                on_submitted=lambda task_id: print(PORTAL + task_id, flush=True),
                approve=not args.hold_review,
            )
    except httpx.HTTPStatusError as exc:
        print(f"Demo API returned HTTP {exc.response.status_code}", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("Unable to reach the demo API", file=sys.stderr)
        return 1
    except (TimeoutError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Awaiting review" if args.hold_review else "Demo succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
