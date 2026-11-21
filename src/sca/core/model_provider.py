"""Classify model failures without carrying upstream diagnostics across trust boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Literal, cast

from sca.core.errors import ScaError

ProviderErrorKind = Literal["billing", "auth", "rate_limit", "unavailable"]

_MESSAGES = {
    "billing": "Provider balance or quota exhausted",
    "auth": "Provider rejected the API credential",
    "rate_limit": "Provider rate limit reached",
    "unavailable": "Model provider unavailable",
}
_ACCOUNT_REASONS = {
    "billing": ("模型服务账户余额不足", "请充值或恢复额度后重试"),
    "auth": ("模型密钥无效", "请更新模型服务密钥后重试"),
}
_STATUS_FIELDS = ("status", "status_code", "statusCode", "http_status", "code")
_ERROR_FIELDS = ("type", "name", "message", "errorMessage", "error", "detail", "data", "cause",
                 "response", "body", "responseBody", "response_body", "code")
_HTTP_STATUS = re.compile(
    r'(?:\bhttp(?:/\d(?:\.\d)?)?\s+(?:error\s+)?|\b(?:status(?:[ _-]*code)?)["\']?\s*[:=]?\s*|^)'
    r'["\']?(\d{3})\b', re.IGNORECASE,
)
_BILLING = re.compile(
    r"\b(?:insufficient (?:balance|quota|credits?)|(?:quota|credits?) (?:has been |is )?(?:exhausted|exceeded)|"
    r"(?:exceeded|exhausted) (?:(?:your|the) )?(?:current )?quota|payment required|billing hard limit|"
    r"credit balance is too low|provider\.(?:quota|billing|free limit))\b"
)
_AUTH = re.compile(
    r"\b(?:(?:invalid|incorrect|expired) (?:provider )?(?:api )?key|authentication(?: error|exception| failed)|"
    r"provider\.auth|invalid credentials|api key (?:is )?(?:invalid|not valid))\b"
)
_RATE_LIMIT = re.compile(r"\b(?:too many requests|rate limit (?:reached|exceeded)|provider\.rate limit)\b")
_UNAVAILABLE = re.compile(
    r"\b(?:time[ -]?out|timed out|service unavailable|provider\.(?:transport|timeout|internal)|"
    r"econnreset|econnrefused|etimedout)\b"
)
_GATEWAY_BUDGET = re.compile(r"\b(?:budgetexceedederror|budget (?:has been )?exceeded|exceeded budget)\b")


class GatewayError(ScaError):
    pass


class ModelProviderError(GatewayError):
    def __init__(self, kind: ProviderErrorKind, status: int | None = None) -> None:
        self.kind = kind
        self.status = status
        # Provider bodies can echo credentials and prompts. Only these fixed messages leave parsing.
        self.provider_message_redacted = _MESSAGES[kind]
        super().__init__(self.provider_message_redacted + (f" (HTTP {status})" if status else ""))

    @property
    def is_account_error(self) -> bool:
        return self.kind in _ACCOUNT_REASONS

    @property
    def reason(self) -> str:
        if not self.is_account_error:
            return str(self)
        message, action = _ACCOUNT_REASONS[self.kind]
        status = f"（{self.status}）" if self.status else ""
        return f"{message}{status}，{action}"

    def payload(self) -> dict:
        return {"kind": self.kind, "status": self.status, "message": self.provider_message_redacted}

    @classmethod
    def from_payload(cls, payload: object) -> ModelProviderError | None:
        if isinstance(payload, dict) and valid_provider_kind(payload.get("kind")):
            return cls(cast(ProviderErrorKind, payload["kind"]), _status(payload.get("status")))
        return None


def valid_provider_kind(value: object) -> bool:
    return isinstance(value, str) and value in _MESSAGES


def _status(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{3}", value):
        value = int(value)
    return value if isinstance(value, int) and 100 <= value <= 599 else None


def _error_fields(value: object, depth: int = 0) -> Iterator[str | int]:
    if isinstance(value, str):
        yield value[:16384]
    elif isinstance(value, dict) and depth < 5:
        for key in _STATUS_FIELDS:
            if (status := _status(value.get(key))) is not None:
                yield status
        for key in _ERROR_FIELDS:
            yield from _error_fields(value.get(key), depth + 1)


def _normalized(text: str) -> str:
    return text.lower().replace("_", " ").replace("-", " ")


def gateway_budget_refused(error: object) -> bool:
    return any(_GATEWAY_BUDGET.search(_normalized(field)) for field in _error_fields(error)
               if isinstance(field, str))


def classify_model_error(error: object = None, *, status: int | None = None) -> ModelProviderError | None:
    fields = list(_error_fields(error))
    messages = [field for field in fields if isinstance(field, str)]
    if status is None:
        status = next((field for field in fields if isinstance(field, int)), None)
    if status is None:
        status = next((int(match[1]) for message in messages if (match := _HTTP_STATUS.search(message))), None)
    text = _normalized("\n".join(messages))
    if status == 402:
        return ModelProviderError("billing", status)
    if _GATEWAY_BUDGET.search(text):
        return None
    if _BILLING.search(text):
        return ModelProviderError("billing", status)
    if status in (401, 403) or _AUTH.search(text):
        return ModelProviderError("auth", status)
    if status == 429 or _RATE_LIMIT.search(text):
        return ModelProviderError("rate_limit", status)
    if status == 408 or (status is not None and 500 <= status <= 599) or _UNAVAILABLE.search(text):
        return ModelProviderError("unavailable", status)
    return None
