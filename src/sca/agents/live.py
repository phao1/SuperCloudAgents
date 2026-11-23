"""Bounded, backend-neutral starts for the Portal status strip."""

from sca.core.models import AgentEvent
from sca.history.privacy import clean_text


def operation_id(value: object) -> str | None:
    return clean_text(value) if isinstance(value, str) and 0 < len(value) <= 256 else None


def activity_start(operation: str, detail: object = None, call_id: object = None) -> AgentEvent:
    data = {"operation": operation, "status": "started"}
    if isinstance(detail, str):
        data["detail"] = clean_text(detail)[:800]
    if id_ := operation_id(call_id):
        data["call_id"] = id_
    return AgentEvent(type="activity", data=data)
