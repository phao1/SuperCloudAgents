import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ValidationError

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.authz import authorize
from sca.api.model_access import require_model
from sca.core.errors import InvalidTaskError, NotFoundError
from sca.core.models import Principal, Task
from sca.gateway.configuration import local_default_model
from sca.kernel.kind import get
from sca.observability import current_trace_id

router = APIRouter(prefix="/v1")


def new_empty_repository(data_dir: Path) -> str:
    """Create a server-side git repository with one empty commit on main for a new project."""
    path = data_dir / "new-repos" / uuid4().hex
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    identity = ["-c", "user.name=SCA", "-c", "user.email=sca@localhost", "-c", "commit.gpgsign=false"]
    try:
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env,
                       capture_output=True, timeout=30)
        subprocess.run(["git", *identity, "-C", str(path), "commit", "-q", "--allow-empty",
                        "-m", "Initial empty commit"], check=True, env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(path, ignore_errors=True)
        raise InvalidTaskError("repo_url: 新建空仓库失败") from exc
    return path.resolve().as_posix()


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
    if not scripted:
        require_model()
    kind = get(body.kind)
    store = request.app.state.store.for_tenant(user.tenant_id)

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
        if body.kind == "coding" and not str(values.get("repo_url") or "").strip():
            if settings is None:
                raise InvalidTaskError("repo_url: 未配置数据目录，无法新建空仓库")
            values = {**values, "repo_url": new_empty_repository(settings.data_dir),
                      "base_ref": "main"}
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
        return {**spec.model_dump(), "priority": body.priority}

    if body.kind == "analysis":
        for file_id in parse(body.spec).inputs:
            try:
                await store.get_file(file_id)
            except NotFoundError as exc:
                raise InvalidTaskError("Input file not found in tenant") from exc
    task, created = await store.create_task(
        tenant_id=user.tenant_id,
        kind=body.kind,
        spec=body.spec,
        idempotency_key=idempotency_key,
        actor=user.subject,
        project_id=body.project_id,
        validate_spec=validate,
    )
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


