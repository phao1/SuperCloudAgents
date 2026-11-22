"""Submit a model-free task to a running source-mode Portal."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("SCA_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--wait", type=float, default=120)
    args = parser.parse_args()
    token = os.environ.get("PORTAL_TOKEN") or os.environ.get("SCA_EVAL_API_TOKEN")
    if not token:
        parser.error("Set PORTAL_TOKEN to the current Portal login token.")
    try:
        with tempfile.TemporaryDirectory(prefix="sca source smoke ") as temporary, httpx.Client(
            base_url=args.api, headers={"Authorization": f"Bearer {token}"},
            timeout=10, trust_env=False,
        ) as client:
            task_id = smoke(client, Path(temporary) / "repo", args.wait)
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f"Source smoke received HTTP {exc.response.status_code}.") from None
    except httpx.RequestError:
        raise SystemExit("Cannot reach the source-mode Portal API.") from None
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Portal loaded and task {task_id} SUCCEEDED with the expected patch.")


if __name__ == "__main__":
    main()
