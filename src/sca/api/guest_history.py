import re
from typing import Any, cast
from urllib.parse import quote

from sca.api.authz import ForbiddenError, GuestPrincipal
from sca.core.models import Artifact, Event, Principal, Task
from sca.secrets import redact

_PRIVATE_FIELD = re.compile(
    r"(?:^|_)(?:secret|secrets|credentials?|password|passwd|token|authorization|"
    r"cookies?|email|api_?key|private_key|master_key|access_key|signing_key|"
    r"customer_?id|idempotency_key)(?:_|$)"
)
_PRIVATE_OBJECTS = {
    "actor", "created_by", "updated_by", "reviewed_by", "approved_by",
    "env", "environment", "headers", "raw", "database_url", "connection_string",
}
_EMAIL_LOCAL = r"[\w.!#$%&'*+/=?^`{|}~-]"
_PRIVATE_TEXT = re.compile(
    rf"(?<!{_EMAIL_LOCAL}){_EMAIL_LOCAL}+@[\w-]+(?:\.[\w-]+)+"
    r"|\b(?:sk[-_]|gh[pousr]_|github_pat_|cus_)[A-Za-z0-9_-]{8,}\b"
    r"|\bsca_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"
    r"|(?i:\bbearer[ \t]+|\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?basic[ \t]+)[A-Za-z0-9._~+/=-]+"
)
_FIELD_VALUE = re.compile(
    r"(?<![\w.-])(?P<key>[\w.-]+)(?P<separator>[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<value>\"(?:\\.|[^\"\\\n])*\"|'[^'\n]*'|[^\s,;\"'}]+)"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)", re.DOTALL,
)
_BINARY_PATCH = re.compile(r"^GIT binary patch\n.*?(?=^diff --git |\Z)", re.MULTILINE | re.DOTALL)


def _private_field(key: str, value: Any) -> bool:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("-", "_").replace(".", "_")
    return (name in _PRIVATE_OBJECTS or bool(_PRIVATE_FIELD.search(name))
            or (name == "tokens" and not isinstance(value, (int, float))))


def _redact_assignment(match: re.Match[str]) -> str:
    key = match["key"]
    if _private_field(key, match["value"]):
        return f"{key}{match['separator']}[redacted]"
    return match[0]


def _text(value: str) -> str:
    value = _BINARY_PATCH.sub("Binary files differ\n", value)
    value = _PRIVATE_KEY.sub("[redacted]", redact(value))
    value = _PRIVATE_TEXT.sub("[redacted]", value)
    return _FIELD_VALUE.sub(_redact_assignment, value)


def _payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {_text(key): _payload(item) for key, item in value.items() if not _private_field(key, item)}
    if isinstance(value, list):
        return [_payload(item) for item in value]
    if isinstance(value, str):
        return _text(value)
    return value


def history_payload[T](user: Principal, value: T) -> T:
    return cast(T, _payload(value)) if isinstance(user, GuestPrincipal) else value


def history_task(user: Principal, task: Task) -> Task:
    if not isinstance(user, GuestPrincipal):
        return task
    return task.model_copy(update={
        "spec": _payload(task.spec), "result": _payload(task.result), "usage": _payload(task.usage),
        "state_reason": _payload(task.state_reason), "idempotency_key": None,
    })


def history_event(user: Principal, event: Event) -> Event:
    if not isinstance(user, GuestPrincipal):
        return event
    return event.model_copy(update={"payload": _payload(event.payload)})


def guest_downloadable(artifact: Artifact) -> bool:
    media = artifact.content_type.partition(";")[0].lower()
    return _text(artifact.name) == artifact.name and (media.startswith("text/") or media == "application/json")


def history_artifacts(user: Principal, artifacts: list[Artifact]) -> list[Artifact]:
    if not isinstance(user, GuestPrincipal):
        return artifacts
    return [artifact.model_copy(update={
        "uri": f"/v1/tasks/{artifact.task_id}/artifacts/{quote(artifact.name)}",
    }) for artifact in artifacts if guest_downloadable(artifact)]


def guest_artifact_text(data: bytes) -> str:
    if b"\0" not in data:
        try:
            return _text(data.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    raise ForbiddenError("访客只能读取文本制品")
