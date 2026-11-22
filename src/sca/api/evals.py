"""Evaluation API for the dedicated eval tenant."""

import asyncio
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound
from temporalio.client import Client
from temporalio.service import RPCError

from sca.agents.opencode_prompt import PROMPT_SUITE, version_commit
from sca.api.authz import ForbiddenError, authorize, principal
from sca.api.guest_history import history_payload
from sca.api.model_access import require_model
from sca.config import get_settings
from sca.core.models import Principal
from sca.db.schema import eval_runs
from sca.eval.compare import compare
from sca.eval.rescore import rescore_run
from sca.eval.suites import Split, case_metadata, load_suite, matches_suite_hash, select_cases
from sca.eval.trajectory import is_errored, overall_passed
from sca.eval.workflow import EvalRunWorkflow
from sca.kernel.prompts import EvaluationSettings, validate_prompt_version

router = APIRouter(prefix="/v1/evals")


def require_eval(user: Principal) -> None:
    if user.tenant_id != "eval":
        raise ForbiddenError("Evaluation requires tenant eval")


def suite_path(name: str) -> Path:
    root = Path(getattr(get_settings(), "eval_suites_dir", Path("evals"))).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise HTTPException(422, "Invalid suite name")
    candidate = root / name
    if candidate.is_symlink() or candidate.resolve().parent != root or not candidate.is_dir():
        raise HTTPException(404, "Suite not found")
    return candidate


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_backend: str | None = None
    agent_version: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    agent_overlay: dict[str, Any] | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: str
    split: Split | None = None
    variant: Variant = Field(default_factory=Variant)
    repeat: int = Field(default=1, ge=1, le=100)
    concurrency: int = Field(default=1, ge=1, le=100)
    budget: float = Field(gt=0, allow_inf_nan=False)
    case_budget: float = Field(gt=0, allow_inf_nan=False)


async def read_run(request: Request, run_id: UUID) -> dict:
    try:
        run = await request.app.state.store.for_tenant("eval").get_eval_run(str(run_id))
        metadata = run["variant"].get("case_metadata", {})
        # NUMERIC columns arrive as Decimal, which JSON would render as long strings.
        return {**run, "split": run["variant"].get("split"),
                "results": [{**row, **metadata.get(row["case_id"], {}),
                             "cost_usd": float(row["cost_usd"] or 0)}
                            for row in run["results"]]}
    except NoResultFound:
        raise HTTPException(404, "Evaluation run not found") from None


def summary(run: dict) -> dict:
    rows = run["results"]
    scored = [row for row in rows if not is_errored(row)]
    return {**{k: v for k, v in run.items() if k != "results"},
            "passed": sum(overall_passed(row) for row in scored),
            "failed": sum(not overall_passed(row) for row in scored),
            "errored": len(rows) - len(scored),
            "infrastructure_errors": sum(bool(row.get("infrastructure_error")) for row in rows),
            "result_count": len(rows), "cost_usd": sum(row["cost_usd"] for row in rows)}


@router.get("/suites")
async def suites(user: Annotated[Principal, Depends(authorize("read", guest=True))]) -> list[dict]:
    require_eval(user)
    root = Path(getattr(get_settings(), "eval_suites_dir", Path("evals")))

    def listing() -> list[dict]:
        result = []
        for path in sorted(root.iterdir()) if root.is_dir() else []:
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                suite = load_suite(suite_path(path.name))
            except (ValueError, OSError, HTTPException):
                continue
            result.append({"name": path.name, "description": suite.get("description", ""),
                           "kind": suite.get("kind", "coding"),
                           "case_metadata": case_metadata(suite["cases"]),
                           "cases": [case["id"] for case in suite["cases"]]})
        return result

    return history_payload(user, await asyncio.to_thread(listing))


@router.post("/runs", status_code=201)
async def start_run(body: RunRequest, request: Request,
                    user: Annotated[Principal, Depends(authorize("submit"))]) -> dict:
    require_eval(user)
    return await enqueue_run(body, request, user)


async def enqueue_run(
    body: RunRequest, request: Request, user: Principal, *,
    expected_suite_hash: str | None = None, source_build_id: UUID | None = None,
) -> dict:
    require_model()
    path = suite_path(body.suite)
    try:
        suite = await asyncio.to_thread(load_suite, path)
        cases = select_cases(suite, body.split)
    except (ValueError, OSError):
        raise HTTPException(422, "Invalid suite") from None
    settings = get_settings()
    if expected_suite_hash is not None and not matches_suite_hash(suite, expected_suite_hash):
        raise HTTPException(409, "套件内容已变化，请用当前套件重新运行基线")
    variant = body.variant.model_dump(exclude_none=True)
    if body.suite == PROMPT_SUITE:
        if body.variant.agent_backend != "opencode" or not body.variant.model:
            raise HTTPException(422, "系统提示词评测需要 OpenCode 和模型")
        try:
            version_commit(settings, body.variant.agent_version or "")
            prompt_version = body.variant.prompt_version or EvaluationSettings().default_prompt_version
            validate_prompt_version("coding", prompt_version)
        except (OSError, ValueError):
            raise HTTPException(422, "请选择已安装的 OpenCode 版本和有效的任务提示词版本") from None
        variant["prompt_version"] = prompt_version
    run_id = str(uuid4())
    payload = {**body.model_dump(), "id": run_id, "suite": str(path.resolve()),
               "suite_hash": suite["hash"], "variant": variant,
               "case_metadata": case_metadata(cases)}
    if source_build_id is not None:
        payload["source_build_id"] = str(source_build_id)
    await request.app.state.store.for_tenant("eval").create_eval_run({
        "id": UUID(run_id), "tenant_id": "eval", "suite": suite["name"],
        # Retests remain comparable to their verified baseline, including legacy hashes.
        "suite_hash": expected_suite_hash or suite["hash"], "variant": payload,
        "created_by": user.subject, "status": "queued",
    })
    try:
        async with asyncio.timeout(10):
            client = getattr(request.app.state.orchestrator, "client", None)
            if client is None:
                client = await Client.connect(settings.temporal_address,
                                              namespace=settings.temporal_namespace)
            await client.start_workflow(EvalRunWorkflow.run, payload, id=f"eval-{run_id}",
                                        task_queue=settings.task_queue)
    except (TimeoutError, RuntimeError, RPCError, OSError):
        # The row exists already; without a workflow it would stay queued forever.
        await request.app.state.store.for_tenant("eval").finish_eval_run(run_id, "failed")
        raise HTTPException(503, "Evaluation service unavailable") from None
    return {"id": run_id}


@router.get("/runs")
async def runs(request: Request, user: Annotated[Principal, Depends(authorize("read", guest=True))],
               limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    require_eval(user)
    store = request.app.state.store.for_tenant("eval")
    async with store.sessionmaker.begin() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', 'eval', true)"))
        ids = (await session.execute(select(eval_runs.c.id).where(
            eval_runs.c.tenant_id == "eval").order_by(eval_runs.c.created_at.desc()).limit(limit))).scalars().all()
    return history_payload(user, [summary(await read_run(request, run_id)) for run_id in ids])


@router.get("/runs/{run_id}")
async def detail(run_id: UUID, request: Request,
                 user: Annotated[Principal, Depends(authorize("read", guest=True))]) -> dict:
    require_eval(user)
    run = await read_run(request, run_id)
    return history_payload(user, {**summary(run), "results": run["results"]})


@router.get("/compare")
async def comparison(a: UUID, b: UUID, request: Request,
                     user: Annotated[Principal, Depends(authorize("read", guest=True))]) -> dict:
    require_eval(user)
    try:
        return history_payload(user, compare(await read_run(request, a), await read_run(request, b)))
    except ValueError:
        raise HTTPException(422, "Runs have different suite contents") from None


def can_rescore(user: Principal) -> bool:
    if user.tenant_id != "eval":
        return False
    return "eval:admin" in user.scopes if user.scopes else user.role == "admin"


@router.post("/runs/{run_id}/rescore")
async def rescore(run_id: UUID, request: Request,
                  user: Annotated[Principal, Depends(principal)]) -> dict:
    require_eval(user)
    if not can_rescore(user):
        raise ForbiddenError("Rescore requires eval:admin or tenant admin")
    run = await read_run(request, run_id)
    if run["status"] not in {"completed", "budget_exhausted"}:
        raise HTTPException(409, "Evaluation run is still active")
    try:
        suite = await asyncio.to_thread(load_suite, suite_path(run["suite"]))
        await rescore_run(request.app.state.store.for_tenant("eval"), run, suite)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, "Cannot load trajectory rules for this run") from exc
    return await detail(run_id, request, user)
