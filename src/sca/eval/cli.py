"""CLI orchestration and reporting for evaluations."""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import typer
from temporalio.client import Client

from sca.config import get_settings
from sca.db.engine import make_engine, make_sessionmaker
from sca.db.repo import Store
from sca.eval.compare import compare, markdown
from sca.eval.suites import load_suite
from sca.eval.workflow import EvalRunWorkflow

app = typer.Typer(no_args_is_help=True)


async def read_run(run_id: str) -> dict:
    engine = make_engine(get_settings().database_url)
    try:
        return await Store(make_sessionmaker(engine)).get_eval_run(run_id)
    finally:
        await engine.dispose()


async def start(suite: Path, model: str | None, prompt_version: str | None, repeat: int,
                concurrency: int, budget: float, case_budget: float,
                agent_backend: str | None, memory: bool, wait: bool,
                intents: bool = True, plan: str | None = None) -> str:
    settings = get_settings()
    run_id = str(uuid4())
    variant = {k: v for k, v in {"model": model, "prompt_version": prompt_version,
                                "agent_backend": agent_backend, "memory_enabled": memory, "plan": plan}.items()
               if v is not None}
    if not intents:
        variant["intents"] = []
    request = {"id": run_id, "suite": str(suite.resolve()), "variant": variant,
               "repeat": repeat, "concurrency": concurrency, "budget": budget,
               "case_budget": case_budget}
    loaded = await asyncio.to_thread(load_suite, suite)
    request["suite_hash"] = loaded["hash"]
    engine = make_engine(settings.database_url)
    try:
        await Store(make_sessionmaker(engine)).for_tenant("eval").create_eval_run({
            "id": UUID(run_id), "tenant_id": "eval", "suite": loaded["name"],
            "suite_hash": loaded["hash"], "variant": request,
            "created_by": "cli", "status": "queued",
        })
    finally:
        await engine.dispose()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(EvalRunWorkflow.run, request, id=f"eval-{run_id}",
                                         task_queue=settings.task_queue)
    typer.echo(run_id)
    if wait:
        await handle.result()
    return run_id


@app.command("run")
def run(suite: Path, model: str | None = None, prompt_version: str | None = None,
        repeat: int = typer.Option(3, min=1), concurrency: int = typer.Option(1, min=1),
        budget: float = typer.Option(1.0, min=0.000001),
        case_budget: float = typer.Option(.05, min=0.000001),
        agent_backend: str | None = None, memory: bool = True, wait: bool = False,
        intents: bool = True, plan: str | None = None) -> None:
    asyncio.run(start(suite, model, prompt_version, repeat, concurrency, budget, case_budget,
                      agent_backend, memory, wait, intents, plan))


@app.command("show")
def show(run_id: str) -> None:
    typer.echo(json.dumps(asyncio.run(read_run(run_id)), default=str, indent=2))


@app.command("compare")
def compare_runs(a: str, b: str, markdown_output: bool = typer.Option(False, "--markdown")) -> None:
    async def report() -> dict:
        runs = await asyncio.gather(read_run(a), read_run(b))
        return compare(*runs)
    result = asyncio.run(report())
    typer.echo(markdown(result) if markdown_output else json.dumps(result, indent=2))


@app.command("gate")
def gate(suite: Path, baseline: str = typer.Option(...), min_pass_rate_delta: float = -.05,
         model: str | None = None, prompt_version: str | None = None,
         repeat: int = typer.Option(3, min=1), concurrency: int = typer.Option(1, min=1),
         budget: float = typer.Option(1.0, min=0.000001),
         case_budget: float = typer.Option(.05, min=0.000001)) -> None:
    async def check() -> bool:
        run_id = await start(suite, model, prompt_version, repeat, concurrency, budget,
                             case_budget, None, True, True)
        before, after = await asyncio.gather(read_run(baseline), read_run(run_id))
        result = compare(before, after)
        typer.echo(markdown(result))
        if before["status"] != "completed" or after["status"] != "completed":
            return False
        return result["B"]["pass_rate"]["mean"] - result["A"]["pass_rate"]["mean"] >= min_pass_rate_delta
    if not asyncio.run(check()):
        raise typer.Exit(1)


@app.command("rescore")
def rescore(run_id: str) -> None:
    """Recompute trajectory checks from stored events using current suite rules."""
    from sca.eval.rescore import rescore_run
    from sca.eval.suites import load_suite

    async def execute() -> dict:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        try:
            store = Store(make_sessionmaker(engine)).for_tenant("eval")
            run = await store.get_eval_run(run_id)
            if run["status"] not in {"completed", "budget_exhausted"}:
                raise ValueError("Evaluation run is still active")
            root = settings.eval_suites_dir.resolve()
            path = root / run["suite"]
            if path.is_symlink() or path.resolve().parent != root:
                raise ValueError("Invalid suite name")
            suite = await asyncio.to_thread(load_suite, path)
            await rescore_run(store, run, suite)
            return await store.get_eval_run(run_id)
        finally:
            await engine.dispose()
    typer.echo(json.dumps(asyncio.run(execute()), default=str, indent=2))
