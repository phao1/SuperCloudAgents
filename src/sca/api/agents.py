from __future__ import annotations

import asyncio
import difflib
import json
import shlex
from collections import Counter
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import NoResultFound
from temporalio.service import RPCError

from sca.agents.opencode_build import (
    DOCKER_BUILD_REQUIRED,
    records_dir,
    reserve_build,
    write_record,
)
from sca.agents.opencode_prompt import (
    PROMPT_MODELS,
    PROMPT_PATH,
    PROMPT_SUITE,
    PROMPT_TARGET,
    TOOL_GUIDANCE,
    is_commit,
    read_source_prompt,
    version_commit,
)
from sca.api.authz import authorize
from sca.api.evals import RunRequest, enqueue_run, read_run, require_eval
from sca.api.evolutions import Admin, client_for, scoped
from sca.api.model_access import require_model
from sca.config import Settings, get_settings
from sca.core.errors import InvalidTaskError, NotFoundError
from sca.core.models import Event, Principal, Task
from sca.eval.trajectory import overall_passed
from sca.evolve.solidify import SOLIDIFY_KIND, ChampionSolidifyWorkflow, champion_branch
from sca.kinds.coding.spec import CodingSpec

router = APIRouter(prefix="/v1/agents/opencode")
TEST_COMMAND = "cd packages/core && bun test test/tool-edit.test.ts test/tool-write.test.ts"
SETUP_COMMAND = "true"


class FixRequest(BaseModel):
    failing_task_id: UUID
    agent_backend: Literal["codex", "opencode"]
    target: Literal["source", "system_prompt"] = "source"
    model: str | None = None
    agent_version: str | None = Field(default=None, pattern=r"^2\.3\.0-sca\.(0|[1-9][0-9]*)$")
    extra_instruction: str | None = Field(default=None, max_length=10000)


class BuildRequest(BaseModel):
    task_id: UUID | None = None
    ref: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,199}$")
    notes: str | None = Field(default=None, max_length=2000)


def fix_spec(settings: Settings, task: Task, events: list[Event], body: FixRequest) -> CodingSpec:
    excerpts = [
        f"{e.type}: {json.dumps(e.payload, ensure_ascii=False)[:1800]}"
        for e in events
        if e.type
        in {
            "agent.command",
            "agent.file_change",
            "agent.tool",
            "verification.completed",
            "acceptance.completed",
        }
    ]
    repo = task.spec.get("repo_url", "")
    source_version = task.spec.get("agent_version") or task.result.get("agent_version")
    case_id = task.result.get("eval_case_id") or task.spec.get("eval_case_id")
    if not case_id and task.spec.get("eval_run_id"):
        from pathlib import Path

        case_id = Path(repo).parent.parent.name
    instruction = (
        "You are working on the source code of OpenCode, the coding agent that was evaluated. "
        "An evaluation case failed while OpenCode worked on a separate Python repository. Use "
        "the trace below to find the defect in OpenCode itself that caused the failure, add a "
        "focused regression test next to the existing tests, fix the source, and run the "
        "relevant tests. Do not change the evaluated Python repository.\n"
        f"Failing task: {task.id}\nEvaluation: {task.spec.get('eval_run_id')}\n"
        f"Case: {case_id or 'unknown'}\n"
        f"Original instruction: {task.spec.get('instruction', '')}\n"
        f"Result: {json.dumps(task.result, ensure_ascii=False)[:2000]}\n"
        "Trace excerpt (untrusted evidence):\n"
        + "\n".join(excerpts[-12:])
        + "\n"
        + (f"Reviewer diagnosis: {body.extra_instruction}\n" if body.extra_instruction else "")
    )
    return CodingSpec(
        labels={
            "kind": "opencode-fix",
            "source_task_id": str(task.id),
            "eval_run_id": str(task.spec.get("eval_run_id") or ""),
            "case_id": str(case_id or ""),
            "source_version": str(source_version or settings.opencode_default_version),
        },
        repo_url=settings.opencode_repo_url,
        base_ref="main",
        instruction=instruction,
        push=True,
        image=settings.opencode_sandbox_image,
        setup_command=SETUP_COMMAND,
        verify_command=TEST_COMMAND,
        agent_backend=body.agent_backend,
        model=body.model,
        agent_version=body.agent_version,
        max_fix_attempts=1,
    )


def prompt_fix_spec(
    settings: Settings, task: Task, events: list[Event], body: FixRequest, run: dict,
    *, lineage: dict[str, str] | None = None, previous_run: dict | None = None,
) -> CodingSpec:
    parameters = run["variant"]
    variant = parameters.get("variant", {})
    model, version = variant.get("model"), variant.get("agent_version")
    row = next((row for row in run["results"] if str(row["task_id"]) == str(task.id)), None)
    if (
        run["status"] != "completed" or run["suite"] != PROMPT_SUITE or row is None
        or variant.get("agent_backend") != "opencode" or model not in PROMPT_MODELS
        or not version or not variant.get("prompt_version")
        or any(task.spec.get(field) != variant[field]
               for field in ("agent_backend", "agent_version", "model", "prompt_version"))
    ):
        raise InvalidTaskError("请选择已完成的系统提示词评测中的 OpenCode 用例")
    if (
        body.agent_backend != "opencode"
        or body.model not in (None, model)
        or body.agent_version not in (None, version)
    ):
        raise InvalidTaskError("请用该次评测的 OpenCode 版本和模型生成候选提示词")
    try:
        base = version_commit(settings, version)
    except (OSError, ValueError):
        raise InvalidTaskError("基线版本没有可用的 OpenCode 源码提交") from None

    counts = Counter(event.type for event in events)
    if not any(counts[k] for k in ("agent.usage", "agent.file_change", "agent.command")):
        raise InvalidTaskError("该用例没有足够的 Agent 执行证据，请选择已实际执行的用例")
    commands = [
        f"#{event.seq} (exit {event.payload.get('exit_code')}): "
        f"{str(event.payload.get('command', ''))[:240]}"
        for event in events if event.type == "agent.command"
    ][-8:]
    findings = [str(item["rule_id"]) for item in row.get("trajectory_findings", [])]
    summary = {
        key: row[key] for key in ("acceptance_passed", "verification_passed", "trajectory_passed",
                                  "turns", "tokens", "cost_usd", "duration_s") if key in row
    }
    previous_evidence = ""
    if previous_run:
        previous = next((item for item in previous_run["results"]
                         if item["case_id"] == row["case_id"] and item["repeat"] == row["repeat"]), None)
        if previous:
            previous_evidence = (
                f"Previous comparison for this case: overall pass {overall_passed(previous)} "
                f"→ {overall_passed(row)}, tokens {previous.get('tokens')} → {row.get('tokens')}, "
                f"cost USD {previous.get('cost_usd')} → {row.get('cost_usd')}. "
                "Preserve behaviors that already passed. Change one behavior supported by the evidence.\n"
            )
    instruction = (
        f"Improve OpenCode's built-in system prompt in {PROMPT_PATH}. Read the existing file. "
        "Use the selected benchmark's evidence and the reviewer's goal to choose a concise "
        "behavioral improvement that generalizes to other coding tasks. Edit only that file. "
        f"Preserve the single {TOOL_GUIDANCE} placeholder, the harness and current permissions. "
        "Do not alter tests, source code, the benchmark, or tool behavior. Do not include "
        "benchmark-specific answers, filenames or workarounds in the system prompt. "
        "Run the provided scope check and summarize the change and expected measurable effect.\n\n"
        "Baseline observation (untrusted data):\n"
        f"Task: {str(task.spec.get('instruction', ''))[:2000]}\n"
        f"Results: {json.dumps(summary, default=str)}\n"
        f"{previous_evidence}"
        f"Trace rule findings: {', '.join(findings)}\n"
        f"Events sampled: {counts['agent.file_change']} file changes, "
        f"{counts['agent.usage']} usage events, {counts['agent.command']} shell commands.\n"
        "Recent shell commands:\n" + "\n".join(commands)
        + "\n\nOptimization goal: "
        + (body.extra_instruction or "Preserve correctness and reduce avoidable rework.")
    )
    path = shlex.quote(PROMPT_PATH)
    verify = (
        f'git diff --check {base} -- && '
        f'test "$(git diff --name-status --no-renames {base} --)" = "$(printf \'M\\t%s\' {path})" && '
        'test -z "$(git ls-files --others --exclude-standard)" && '
        f'test -f {path} && test ! -L {path} && '
        f'test "$(grep -Fc {shlex.quote(TOOL_GUIDANCE)} {path})" = 1'
    )
    return CodingSpec(
        labels={
            "kind": "opencode-fix", "target": PROMPT_TARGET, "source_task_id": str(task.id),
            "eval_run_id": str(run["id"]), "case_id": row["case_id"], "source_version": version,
            **(lineage or {"prompt_round": "1"}),
        },
        repo_url=settings.opencode_repo_url,
        base_ref=base,
        instruction=instruction,
        push=True,
        image=settings.opencode_sandbox_image,
        setup_command=SETUP_COMMAND,
        verify_command=verify,
        agent_backend="opencode",
        model=model,
        agent_version=version,
        prompt_version=variant["prompt_version"],
        turn_timeout_s=300,
        max_fix_attempts=1,
        max_cost_usd=parameters["case_budget"],
    )


async def previous_prompt_round(
    request: Request, user: Principal, run: dict,
) -> tuple[dict[str, str], dict | None]:
    params = run["variant"]
    source_build_id = params.get("source_build_id")
    if source_build_id:
        build = await builds_get(UUID(source_build_id), user)
        labels = build.get("labels", {})
        if (build["status"] == "succeeded" and labels.get("target") == PROMPT_TARGET
                and build["version"] == params.get("variant", {}).get("agent_version")
                and build.get("task_id")):
            iteration = int(labels.get("prompt_round", "1")) + 1
            previous = await read_run(request, UUID(labels["eval_run_id"]))
            return {"prompt_round": str(iteration), "previous_fix_id": build["task_id"]}, previous
    return {"prompt_round": "1"}, None


def prompt_build_ready(task: Task) -> bool:
    return task.result.get("verification_passed") is True and task.result.get("pushed") is True


@router.get("/versions")
async def versions(user: Annotated[Principal, Depends(authorize("read"))]) -> list[dict]:
    root = get_settings().opencode_versions_dir
    rows = [json.loads(p.read_text()) for p in root.glob("*/meta.json")]
    return sorted(rows, key=lambda r: r.get("built_at", ""), reverse=True)


@router.get("/fixes")
async def fixes_list(
    request: Request, user: Annotated[Principal, Depends(authorize("read"))]
) -> list[dict]:
    from sqlalchemy import select

    from sca.db.schema import tasks

    store = request.app.state.store.for_tenant(user.tenant_id)
    async with store._transaction() as session:
        rows = (await session.execute(
            select(tasks).where(
                tasks.c.tenant_id == user.tenant_id,
                tasks.c.spec["labels"]["kind"].astext == "opencode-fix",
            ).order_by(tasks.c.created_at.desc())
        )).mappings().all()
    builds = await builds_list(user)
    return [
        {
            "id": row["id"], "state": row["state"], "labels": row["spec"]["labels"],
            "branch": row["result"].get("branch"), "commit": row["result"].get("commit"),
            "created_at": row["created_at"],
            "builds": [b for b in builds if b.get("task_id") == str(row["id"])],
        }
        for row in rows
    ]


@router.post("/fixes", status_code=201)
async def fixes(
    body: FixRequest, request: Request, user: Annotated[Principal, Depends(authorize("submit"))]
) -> Task:
    require_model()
    settings = get_settings()
    _require_docker_build(settings)
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(body.failing_task_id, user.tenant_id)
    if task.kind != "coding":
        raise InvalidTaskError("只能修复编码任务的 Agent")
    run = None
    if body.target == PROMPT_TARGET:
        require_eval(user)
        try:
            run_id = UUID(task.spec.get("eval_run_id") or "")
        except ValueError:
            raise InvalidTaskError("请选择来自系统提示词评测的用例") from None
        run = await read_run(request, run_id)
    events, cursor = [], 0
    while True:
        batch = await store.list_events(task.id, cursor, 500)
        events = (events + batch)[-100:]
        if len(batch) < 500:
            break
        cursor = batch[-1].seq
    if run is None:
        spec = fix_spec(settings, task, events, body)
    else:
        lineage, previous = await previous_prompt_round(request, user, run)
        spec = prompt_fix_spec(settings, task, events, body, run,
                               lineage=lineage, previous_run=previous)
    fixed, _ = await store.create_task(
        tenant_id=user.tenant_id,
        kind="coding",
        spec=spec.model_dump(),
        actor=user.subject,
        idempotency_key=None,
    )
    await request.app.state.orchestrator.start_task(str(fixed.id), fixed.kind)
    return fixed


@router.get("/fixes/{task_id}/prompt")
async def prompt_snapshot(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read"))],
) -> dict:
    require_eval(user)
    task = await request.app.state.store.for_tenant(user.tenant_id).get_task(task_id, user.tenant_id)
    settings = get_settings()
    labels = task.spec.get("labels", {})
    if labels.get("target") != PROMPT_TARGET or task.spec.get("repo_url") != settings.opencode_repo_url:
        raise NotFoundError("系统提示词修改任务不存在")
    base_commit = task.spec.get("base_ref")
    commit = task.result.get("commit")
    try:
        base = await read_source_prompt(settings, base_commit)
    except (OSError, ValueError, TimeoutError):
        raise NotFoundError("无法读取基线系统提示词") from None
    candidate = None
    if commit and (task.result.get("pushed") or commit == base_commit):
        try:
            candidate = await read_source_prompt(settings, commit)
        except (OSError, ValueError, TimeoutError):
            pass
    patch = "".join(difflib.unified_diff(
        base.splitlines(keepends=True), candidate.splitlines(keepends=True), "基线", "候选",
    )) if candidate is not None else ""
    return {
        "path": PROMPT_PATH, "base": base, "candidate": candidate, "diff": patch,
        "base_commit": base_commit, "candidate_commit": commit, "turn": task.result.get("turns"),
        "verified": prompt_build_ready(task) and candidate is not None,
    }


@router.post("/builds", status_code=202)
async def builds_start(
    body: BuildRequest, request: Request, user: Annotated[Principal, Depends(authorize("quota"))]
) -> dict:
    settings = get_settings()
    _require_docker_build(settings)
    ref = body.ref or "main"
    labels = {}
    build_fields = {}
    if body.task_id:
        task = await request.app.state.store.for_tenant(user.tenant_id).get_task(
            body.task_id, user.tenant_id
        )
        if task.spec.get("repo_url") != settings.opencode_repo_url or not task.spec.get("push"):
            raise InvalidTaskError("构建任务必须推送到 OpenCode 仓库")
        if task.state != "SUCCEEDED":
            raise InvalidTaskError("请先审核并完成修复任务，确保分支已推送")
        labels = task.spec.get("labels", {})
        ref = body.ref or task.result.get("commit") or task.result.get("branch")
        if not ref:
            raise InvalidTaskError("修复任务尚无分支")
        if labels.get("target") == PROMPT_TARGET:
            require_eval(user)
            if body.ref is not None or not is_commit(task.result.get("commit")):
                raise InvalidTaskError("系统提示词构建必须使用审核通过的提交")
            if not prompt_build_ready(task):
                raise InvalidTaskError("候选需通过修改范围验证，并推送审核过的分支")
            build_fields["base_version"] = labels.get("source_version")
    record = reserve_build(
        settings,
        str(uuid4()),
        user.tenant_id,
        ref=ref,
        task_id=str(body.task_id) if body.task_id else None,
        notes=body.notes,
        labels=labels,
        **build_fields,
    )
    from sca.agents.opencode_build import OpenCodeBuildWorkflow

    try:
        await request.app.state.orchestrator.client.start_workflow(
            OpenCodeBuildWorkflow.run,
            record,
            id=f"opencode-build-{record['id']}",
            task_queue=settings.task_queue,
        )
    except Exception:
        record["status"] = "failed"
        write_record(records_dir(settings) / f"{record['id']}.json", record)
        raise
    return record


@router.post("/builds/{build_id}/retest", status_code=201)
async def retest(
    build_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("submit"))],
) -> dict:
    require_eval(user)
    require_model()
    build = await builds_get(build_id, user)
    if build["status"] != "succeeded":
        raise HTTPException(409, "请等待 OpenCode 构建成功")
    try:
        baseline_id = UUID(build.get("labels", {}).get("eval_run_id") or "")
    except ValueError:
        raise InvalidTaskError("该构建没有关联的基线评测") from None
    baseline = await read_run(request, baseline_id)
    if baseline["status"] != "completed":
        raise HTTPException(409, "请等待基线评测完成")
    parameters = baseline["variant"]
    variant = parameters.get("variant", {})
    if (build.get("labels", {}).get("target") == PROMPT_TARGET
            and build.get("base_version") != variant.get("agent_version")):
        raise HTTPException(409, "候选提示词版本与基线不匹配")
    body = RunRequest(
        suite=baseline["suite"],
        variant={**variant, "agent_backend": "opencode", "agent_version": build["version"]},
        repeat=parameters["repeat"], concurrency=parameters["concurrency"],
        budget=parameters["budget"], case_budget=parameters["case_budget"],
    )
    return await enqueue_run(body, request, user, expected_suite_hash=baseline["suite_hash"],
                             source_build_id=build_id)


@router.get("/builds")
async def builds_list(user: Annotated[Principal, Depends(authorize("read"))]) -> list[dict]:
    rows = [json.loads(p.read_text()) for p in records_dir(get_settings()).glob("*.json")]
    rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
    return [await builds_get(UUID(r["id"]), user) for r in rows if r["tenant_id"] == user.tenant_id]


@router.get("/builds/{build_id}")
async def builds_get(
    build_id: UUID, user: Annotated[Principal, Depends(authorize("read"))]
) -> dict:
    root = records_dir(get_settings())
    path = root / f"{build_id}.json"
    if not path.exists():
        raise NotFoundError("构建不存在")
    record = json.loads(path.read_text())
    if record["tenant_id"] != user.tenant_id:
        raise NotFoundError("构建不存在")
    log = root / f"{build_id}.log"
    if log.exists():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 8192))
            record["log_tail"] = stream.read().decode(errors="replace")
    else:
        record["log_tail"] = ""
    return record


# Champion solidification and platform default
champions_router = APIRouter(prefix="/v1/champions")


class SolidifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str | None = Field(default=None, max_length=2000)


async def _champion(request: Request, user: Principal, champion_id: UUID) -> dict:
    try:
        champion = await scoped(request, user).get_champion(champion_id)
    except NoResultFound:
        champion = None
    if champion is None:
        raise NotFoundError("冠军记录不存在")
    return champion


@champions_router.get("/{champion_id}")
async def champion_detail(champion_id: UUID, request: Request, user: Admin) -> dict:
    champion = await _champion(request, user, champion_id)
    builds = [build for build in await builds_list(user)
              if build.get("labels", {}).get("kind") == SOLIDIFY_KIND
              and build["labels"].get("champion_id") == str(champion_id)]
    return {**champion, "solidify_builds": builds}


@champions_router.post("/{champion_id}/solidify", status_code=202)
async def solidify(
    champion_id: UUID, request: Request, user: Admin, body: SolidifyRequest | None = None,
) -> dict:
    require_eval(user)
    require_model()
    settings = get_settings()
    _require_docker_build(settings)
    champion = await _champion(request, user, champion_id)
    if not champion.get("candidate_id"):
        raise HTTPException(409, "冠军缺少原始评测记录")
    source_version = champion["agent_version"]
    branch = champion_branch(champion_id)
    record = reserve_build(
        settings, str(uuid4()), user.tenant_id, ref=branch, branch=branch,
        base_version=source_version, task_id=None, notes=body.notes if body else None,
        labels={"kind": SOLIDIFY_KIND, "champion_id": str(champion_id),
                "source_version": source_version},
    )
    try:
        async with asyncio.timeout(10):
            client = await client_for(request)
            await client.start_workflow(
                ChampionSolidifyWorkflow.run, {"record": record, "created_by": user.subject},
                id=f"champion-solidify-{record['id']}", task_queue=settings.task_queue,
            )
    except (OSError, RuntimeError, RPCError):
        message = "固化服务暂不可用"
        write_record(records_dir(settings) / f"{record['id']}.json",
                     {**record, "status": "failed", "error": message})
        raise HTTPException(503, message) from None
    return record


@router.post("/versions/{version}/default")
async def set_default_version(version: str, request: Request, user: Admin) -> dict:
    store = scoped(request, user)
    try:
        version_commit(get_settings(), version)
    except (OSError, ValueError):
        raise NotFoundError("OpenCode 版本不存在或尚未完成构建") from None
    await store.set_default_opencode_version(version)
    return {"version": version}


# Local source mode
def _require_docker_build(settings: Settings) -> None:
    if settings.sandbox_driver == "local":
        raise HTTPException(409, DOCKER_BUILD_REQUIRED)
