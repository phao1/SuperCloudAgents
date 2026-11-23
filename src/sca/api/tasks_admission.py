from dataclasses import asdict
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ValidationError

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.authz import authorize, can_use_output_dir
from sca.api.model_access import require_model
from sca.core.errors import InvalidTaskError, NotFoundError
from sca.core.models import Principal, Task
from sca.gateway.configuration import local_default_model
from sca.kernel.kind import get
from sca.observability import current_trace_id
from sca.workspace.output import prepare_output

router = APIRouter(prefix="/v1")


class OutputDirectoryForbiddenError(InvalidTaskError):
    def __init__(self) -> None:
        super().__init__("仅本机部署支持输出目录")


class Submission(BaseModel):
    kind: str
    spec: dict[str, Any]
    priority: Literal["interactive", "eval", "batch"] = "interactive"
    project_id: UUID | None = None


@router.post("/tasks", status_code=201)
async def submit(
    body: Submission,
    request: Request,
    user: Annotated[Principal, Depends(authorize("submit"))],
    idempotency_key: str | None = Header(None),
) -> Task:
    settings = getattr(request.app.state, "settings", None)
    local = getattr(settings, "sandbox_driver", None) == "local"
    # The local demo administrator can verify the stack before configuring a provider.
    scripted = (local and body.spec.get("agent_backend") == "fake"
                and user.tenant_id == "eval" and user.role == "admin" and not user.scopes)
    coding = body.kind == "coding"
    output_allowed = coding and can_use_output_dir(request, user)
    repo = body.spec.get("repo_url")
    needs_repo = coding and (repo is None or (isinstance(repo, str) and not repo.strip()))
    if coding and body.spec.get("output_dir") is not None and not output_allowed:
        raise OutputDirectoryForbiddenError()
    if not scripted and not needs_repo:
        require_model()
    kind = get(body.kind)
    store = request.app.state.store.for_tenant(user.tenant_id)
    initial_result: dict[str, Any] = {}

    def parse(values: dict) -> BaseModel:
        try:
            return kind.spec_model.model_validate(values)
        except ValidationError as exc:
            message = "; ".join(
                ".".join(map(str, error["loc"])) + ": " + error["msg"]
                for error in exc.errors(include_input=False)
            )
            raise InvalidTaskError(message) from exc

    def validate(values: dict) -> dict:
        new_project = coding and "repo_url" not in values
        if new_project and not output_allowed:
            raise OutputDirectoryForbiddenError()
        if needs_repo and not scripted:
            require_model()
        if new_project:
            values = {**values, "repo_url": "."}
            if not (values.get("subtasks") or values.get("best_of")):
                values.setdefault("plan", "auto")
        elif coding and values.get("output_dir") is not None:
            raise InvalidTaskError("已指定仓库地址的任务不能设置输出目录")
        spec = parse(values)
        if local:
            from sca.agents.local import host_agent_binary, unavailable_source_backend

            backend = spec.agent_backend or settings.agent_backend
            if backend != "fake" and host_agent_binary(settings, backend) is None:
                raise InvalidTaskError(unavailable_source_backend(backend))
        if settings is not None and spec.model is None:
            default = local_default_model(settings.data_dir, settings.default_model)
            if default != settings.default_model:
                spec.model = default
        if new_project:
            if spec.push or spec.ci:
                raise InvalidTaskError("输出目录任务不支持推送、拉取请求或外部 CI")
            spec.repo_url, spec.base_ref, target = prepare_output(
                settings, spec.output_dir, spec.instruction,
                spec.base_ref if "base_ref" in values else None,
            )
            spec.output_dir = spec.repo_url
            # This record authorizes the worker to write to the host. It is never accepted from a spec.
            initial_result["output_target"] = asdict(target)
        return {**spec.model_dump(), "priority": body.priority}

    if body.kind == "analysis":
        for file_id in parse(body.spec).inputs:
            try:
                await store.get_file(file_id)
            except NotFoundError as exc:
                raise InvalidTaskError("Input file not found in tenant") from exc
    submitted = dict(body.spec)
    if needs_repo:
        submitted.pop("repo_url", None)
    task, created = await store.create_task(
        tenant_id=user.tenant_id,
        kind=body.kind,
        spec=submitted,
        idempotency_key=idempotency_key,
        actor=user.subject,
        project_id=body.project_id,
        validate_spec=validate,
        initial_result=initial_result,
    )
    if not created:
        if task.spec.get("output_dir") and not output_allowed:
            raise OutputDirectoryForbiddenError()
        if needs_repo and not scripted:
            require_model()
    if scripted and (task.spec.get("agent_backend") or settings.agent_backend) != "fake":
        # The submission may be an idempotent replay of a model-backed task.
        require_model()
    if created:
        request.app.state.metrics.created.inc()
        trace_id = current_trace_id()
        if trace_id:
            await store.update_result(task.id, {"trace_id": trace_id})
            task = await store.get_task(task.id)
    # Starting again repairs a previous submit whose database write succeeded.
    await request.app.state.orchestrator.start_task(str(task.id), task.kind)
    return task


