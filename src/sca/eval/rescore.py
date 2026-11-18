"""Re-score stored events using the current reviewed suite rules; no agent execution."""

from sca.db.repo import Store
from sca.eval.trajectory import evaluate


async def task_events(store: Store, task_id: str) -> list[dict]:
    events: list[dict] = []
    while True:
        batch = await store.list_events(task_id, after=events[-1]["seq"] if events else 0)
        events.extend(event.model_dump() for event in batch)
        if len(batch) < 500:
            return events


async def rescore_run(store: Store, run: dict, suite: dict) -> None:
    checks = {case["id"]: case["trajectory_checks"] for case in suite["cases"]}
    if any(row["case_id"] not in checks for row in run["results"]):
        raise ValueError("Suite no longer contains all recorded cases")
    scored = []
    for row in run["results"]:
        passed, findings = evaluate(await task_events(store, str(row["task_id"])), checks[row["case_id"]])
        scored.append((row, passed, findings))
    for row, passed, findings in scored:
        await store.update_eval_trajectory(str(run["id"]), row["case_id"], row["repeat"], passed, findings)
