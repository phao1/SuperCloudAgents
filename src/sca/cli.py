import json
from typing import Annotated, Any

import httpx
import typer

from sca.config import get_settings
from sca.core.models import TERMINAL_STATES, TaskState

app = typer.Typer(no_args_is_help=True)


def client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        base_url=settings.api_url,
        headers={"Authorization": f"Bearer {settings.api_token}"},
        timeout=30,
    )


def checked(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        typer.echo(response.text, err=True)
        raise typer.Exit(1)
    return response


def output(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2))


def event_line(event: dict) -> None:
    payload = event["payload"]
    useful = next(
        (
            payload[k]
            for k in (
                "to",
                "text",
                "command",
                "message",
                "decision",
                "final_message",
                "name",
                "passed",
            )
            if k in payload
        ),
        "",
    )
    typer.echo(f"{event['seq']} {event['type']} {useful}")


def history(http: httpx.Client, task_id: str) -> int:
    cursor = 0
    while True:
        batch = checked(http.get(f"/v1/tasks/{task_id}/events", params={"after": cursor})).json()
        for event in batch:
            event_line(event)
            cursor = event["seq"]
        if len(batch) < 500:
            return cursor


def follow_events(task_id: str) -> None:
    with client() as http:
        cursor = history(http, task_id)
        task = checked(http.get(f"/v1/tasks/{task_id}")).json()
        if task["state"] in TERMINAL_STATES | {TaskState.AWAITING_REVIEW}:
            history_after = checked(
                http.get(f"/v1/tasks/{task_id}/events", params={"after": cursor})
            ).json()
            for event in history_after:
                event_line(event)
            return
        with http.stream(
            "GET", f"/v1/tasks/{task_id}/events/stream", params={"after": cursor}, timeout=None
        ) as response:
            if response.is_error:
                response.read()
                checked(response)
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                event_line(event)
                if event["type"] == "task.state_changed" and event["payload"]["to"] in (
                    TERMINAL_STATES | {TaskState.AWAITING_REVIEW}
                ):
                    current = checked(http.get(f"/v1/tasks/{task_id}")).json()
                    if current["state"] in TERMINAL_STATES | {TaskState.AWAITING_REVIEW}:
                        return


@app.command()
def submit(
    repo: Annotated[str, typer.Option()],
    instruction: Annotated[str, typer.Option()],
    verify: str | None = None,
    setup: str | None = None,
    base: str = "main",
    push: bool = False,
    follow: bool = False,
) -> None:
    with client() as http:
        task = checked(
            http.post(
                "/v1/tasks",
                json={
                    "kind": "coding",
                    "spec": {
                        "repo_url": repo,
                        "instruction": instruction,
                        "verify_command": verify,
                        "setup_command": setup,
                        "base_ref": base,
                        "push": push,
                    },
                },
            )
        ).json()
    output(task)
    if follow:
        follow_events(task["id"])


@app.command("list")
def list_tasks() -> None:
    with client() as http:
        output(checked(http.get("/v1/tasks")).json())


@app.command()
def show(task_id: str) -> None:
    with client() as http:
        output(checked(http.get(f"/v1/tasks/{task_id}")).json())


@app.command()
def events(task_id: str, follow: bool = False) -> None:
    if follow:
        follow_events(task_id)
    else:
        with client() as http:
            history(http, task_id)


@app.command()
def review(
    task_id: str,
    decision: str,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
) -> None:
    decision = decision.replace("-", "_")
    if decision not in {"approve", "request_changes", "reject"}:
        raise typer.BadParameter("Use approve, request-changes, or reject")
    with client() as http:
        output(
            checked(
                http.post(
                    f"/v1/tasks/{task_id}/review", json={"decision": decision, "feedback": message}
                )
            ).json()
        )


@app.command()
def cancel(task_id: str) -> None:
    with client() as http:
        output(checked(http.post(f"/v1/tasks/{task_id}/cancel")).json())


@app.command()
def patch(task_id: str) -> None:
    with client() as http:
        typer.echo(checked(http.get(f"/v1/tasks/{task_id}/artifacts/changes.patch")).text, nl=False)


@app.command()
def pause(task_id: str) -> None:
    with client() as http:
        output(checked(http.post(f"/v1/tasks/{task_id}/pause")).json())


@app.command()
def resume(task_id: str) -> None:
    with client() as http:
        output(checked(http.post(f"/v1/tasks/{task_id}/resume")).json())
