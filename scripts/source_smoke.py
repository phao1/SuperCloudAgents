"""Smoke test a model-free task and history transfer in source mode."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import UUID

import httpx


def seed_repo(path: Path) -> None:
    path.mkdir()
    (path / "result.txt").write_bytes(b"before\n")
    for command in (("init", "-b", "main"), ("config", "core.autocrlf", "false"),
                    ("add", "result.txt"), ("commit", "-m", "Seed source smoke fixture")):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "user.name=SCA Smoke", "-c", "user.email=smoke@supercloud.invalid", *command],
            cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )


def smoke(client: httpx.Client, repo: Path, wait_s: float) -> UUID:
    config = client.get("/v1/portal/config").raise_for_status().json()
    if config.get("sandbox_driver") != "local":
        raise ValueError("The Portal must be running in authenticated source mode.")
    if not config.get("gateway_reachable") or not config.get("temporal_reachable"):
        raise ValueError("The Portal cannot reach the native gateway or Temporal.")
    page = client.get("/portal").raise_for_status()
    if "text/html" not in page.headers.get("content-type", "") or "<title>SuperCloudAgents" not in page.text:
        raise ValueError("The Portal HTML did not load.")
    seed_repo(repo)
    response = client.post("/v1/tasks", json={"kind": "coding", "spec": {
        "repo_url": repo.as_posix(),
        "base_ref": "main",
        "agent_backend": "fake",
        "instruction": "Update the fixture.\n\n```sca-fake\nprintf 'ready\\n' > result.txt\n```",
        "verify_command": "grep -qx ready result.txt",
        "memory_enabled": False,
        "auto_finish": True,
    }})
    task_id = UUID(response.raise_for_status().json()["id"])
    route = f"/v1/tasks/{task_id}"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        task = client.get(route).raise_for_status().json()
        match task["state"]:
            case "SUCCEEDED":
                patch = client.get(f"{route}/artifacts/changes.patch").raise_for_status().text
                if task["result"].get("verification_passed") is not True or "+ready" not in patch:
                    raise ValueError(f"Task {task_id} is missing verification or the expected patch.")
                return task_id
            case "FAILED" | "CANCELLED":
                raise ValueError(f"Task {task_id} ended in {task['state']}.")
        time.sleep(1)
    client.post(f"{route}/cancel")
    raise ValueError(f"Timed out waiting for task {task_id}.")


def check_history(client: httpx.Client, task_id: UUID) -> None:
    scripts = Path(__file__).resolve().parent
    if os.name == "nt":
        wrapper = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
                   str(scripts / "portal_source.ps1")]
    else:
        wrapper = ["bash", str(scripts / "portal_source.sh")]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryDirectory(prefix="sca source history ") as temporary:
        archive = str(Path(temporary) / "history.zip")
        for command in ("export-history", "import-history"):
            result = subprocess.run(
                [*wrapper, command, archive], cwd=scripts.parent, env=env, timeout=120, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                raise ValueError(f"{command} failed (exit {result.returncode}).")
        if not any(line.startswith("已导入 0 条历史记录和 ") for line in result.stdout.splitlines()):
            raise ValueError("History re-import did not report 0 new records.")

    tasks = client.get("/v1/tasks").raise_for_status().json()
    if not any(task["id"] == str(task_id) for task in tasks):
        raise ValueError(f"Task {task_id} is missing from /v1/tasks after history re-import.")
    print(f"History re-import reported 0 new records. Task {task_id} is listed in /v1/tasks.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("SCA_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--wait", type=float, default=120)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--task-id-only", action="store_true", help="Print only the task ID on success.")
    mode.add_argument("--check-history", type=UUID, metavar="TASK_ID",
                      help="Export and re-import source history, then verify the task is still listed.")
    args = parser.parse_args()
    token = os.environ.get("PORTAL_TOKEN") or os.environ.get("SCA_EVAL_API_TOKEN")
    if not token:
        parser.error("Set PORTAL_TOKEN to the current Portal login token.")
    try:
        with httpx.Client(
            base_url=args.api, headers={"Authorization": f"Bearer {token}"},
            timeout=10, trust_env=False,
        ) as client:
            if args.check_history is not None:
                check_history(client, args.check_history)
                return
            with tempfile.TemporaryDirectory(prefix="sca source smoke ") as temporary:
                task_id = smoke(client, Path(temporary) / "repo", args.wait)
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"Source smoke received HTTP {exc.response.status_code}.") from None
    except httpx.RequestError:
        raise SystemExit("Cannot reach the source-mode Portal API.") from None
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None
    if args.task_id_only:
        print(task_id)
    else:
        print(f"Portal loaded and task {task_id} SUCCEEDED with the expected patch.")


if __name__ == "__main__":
    main()
