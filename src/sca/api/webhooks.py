from fastapi import APIRouter, HTTPException, Request

from sca.core.errors import ConflictError, InvalidTaskError
from sca.core.models import TaskState
from sca.scm.models import IssueDefaults
from sca.scm.repository import ProviderName
from sca.scm.webhook import SIGNATURE_HEADERS, parse_issue_event, signed_secret_ref

router = APIRouter(prefix="/v1/webhooks")


@router.post("/{provider}", status_code=202)
async def issue_webhook(provider: ProviderName, request: Request) -> dict[str, str]:
    settings = request.app.state.scm_settings
    signature = request.headers.get(SIGNATURE_HEADERS[provider])
    if not signature:
        raise HTTPException(401, "Webhook 签名无效")
    body = await request.body()
    binding = signed_secret_ref(settings, provider, body, signature)
    if binding is None:
        raise HTTPException(401, "Webhook 签名无效")

    if provider == "github":
        event_header = request.headers.get("X-GitHub-Event", "")
    else:
        event_header = request.headers.get("X-Gitea-Event-Type") or request.headers.get(
            "X-Gitea-Event", "",
        )
    try:
        event = parse_issue_event(settings, provider, event_header, body)
    except (TypeError, ValueError):
        raise InvalidTaskError("Webhook 工单数据无效") from None
    if event is None:
        return {"status": "ignored"}

    tenant, secret_ref = binding
    store = request.app.state.store.for_tenant(tenant)
    repo = event.repository
    full_name = repo.full_name.lower()
    integration = await store.find_scm_integration(provider, repo.host, full_name, secret_ref)
    if integration is None:
        return {"status": "ignored"}

    defaults = IssueDefaults.model_validate(integration["default_spec"])
    if not event.matches(defaults):
        return {"status": "ignored"}
    try:
        spec = event.task_spec(defaults).model_dump()
    except ValueError:
        raise InvalidTaskError("SCM 集成无法生成有效的编码任务") from None
    issue = event.issue.model_dump()
    key = f"scm:{integration['id']}:{provider}:{repo.host}/{full_name}#{event.issue.number}"
    if event.comment_id is not None:
        key += f":comment:{event.comment_id}"
    task, created = await store.create_task(
        tenant_id=tenant,
        kind="coding",
        spec=spec,
        idempotency_key=key,
        actor=f"scm:{provider}",
        initial_result={"issue": issue},
    )
    if not created and task.result.get("issue") != issue:
        raise ConflictError("该工单的任务去重键已被占用")
    if created:
        request.app.state.metrics.created.inc()
    if task.state == TaskState.QUEUED:
        await request.app.state.orchestrator.start_task(str(task.id), task.kind)
    return {"status": "accepted", "task_id": str(task.id)}
