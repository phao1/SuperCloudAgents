"""Remove credential fields and known secret forms from display data."""

import re
from typing import Any, BinaryIO

from sca.secrets import redact as redact_repo_credentials

_PRIVATE_FIELDS = {
    "raw", "env", "environment", "headers", "database_url", "connection_string",
    "context_keys", "gateway_test_keys", "authorization", "auth", "auth_config",
    "sandbox_id", "billing", "payment", "stripe",
}
_PRIVATE_NAME = re.compile(
    r"(?:^|_)(?:secret|secrets|credentials?|password|passwd|token|authorization|cookies?|"
    r"api_?keys?|api_tokens?|private_key|access_key|client_key|signing_key|master_key|virtual_keys?|"
    r"idempotency_key|billing|payment|stripe|(?:customer|subscription|invoice)_id)(?:_|$)"
)
_ASSIGNMENT = re.compile(
    r"(?<![\w.-])(?P<key>[\w.-]+)(?P<separator>[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<value>\"(?:\\.|[^\"\\\n])*\"|'[^'\n]*'|"
    r"(?i:basic|bearer)[ \t]+[A-Za-z0-9._~+/=-]+|[^\s,;\"'&}]+)"
)
_PRIVATE_VALUE = re.compile(
    r"\b(?:sk[-_]|gh[pousr]_|github_pat_|(?:AKIA|ASIA))[A-Za-z0-9_-]{8,}\b"
    r"|\bsca_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    r"|\b(?:cus|sub|sub_sched|in|ii|price|pi|pm|cs|bps|ch)_(?:(?:test|live)_)?[A-Za-z0-9]{8,}\b"
    r"|(?i:\bbearer[ \t]+)[A-Za-z0-9._~+/=-]+"
)
_URL_PASSWORD = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@:]+:[^/\s@]+@")
_KEY_BEGIN = "-----BEGIN "
_KEY_END = "-----END "
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)", re.DOTALL,
)
_REDACTED = "[redacted]"
_TEXT_LINE_LIMIT = 1024 * 1024


def _private(key: str, value: Any) -> bool:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("-", "_").replace(".", "_")
    return (
        name in _PRIVATE_FIELDS or _PRIVATE_NAME.search(name) is not None
        or (name == "tokens" and not isinstance(value, (int, float)))
    )


def _mask(length: int) -> str:
    return _REDACTED if length >= len(_REDACTED) else "*" * length


def _assignment(match: re.Match[str]) -> str:
    value = match["value"]
    if not _private(match["key"], 0 if value.isdecimal() else value):
        return match[0]
    quotes = value[0] if value[0] in "\"'" else ""
    masked = _mask(len(value) - 2 * len(quotes))
    return f"{match['key']}{match['separator']}{quotes}{masked}{quotes}"


def clean_text(value: str) -> str:
    value = _PRIVATE_KEY.sub(_REDACTED, value)
    value = _URL_PASSWORD.sub(r"\1", value)
    value = redact_repo_credentials(value).replace("://<redacted>@", "://")
    value = _ASSIGNMENT.sub(_assignment, value)
    return _PRIVATE_VALUE.sub(lambda match: _mask(len(match[0])), value)


def clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {clean_text(key): clean_value(item)
                for key, item in value.items() if not _private(key, item)}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    if isinstance(value, str):
        return clean_text(value)
    return value


def copy_text(source: BinaryIO, target: BinaryIO, *, patch: bool = False) -> None:
    in_key = in_binary_patch = False
    while line := source.readline(_TEXT_LINE_LIMIT + 1):
        if len(line) > _TEXT_LINE_LIMIT:
            while line and not line.endswith(b"\n"):
                line = source.readline(_TEXT_LINE_LIMIT + 1)
            target.write(b"[redacted oversized line]\n")
            continue
        text = line.decode("utf-8", errors="surrogateescape")
        if _KEY_BEGIN in text and "PRIVATE KEY-----" in text:
            in_key = _KEY_END not in text
            target.write(b"[redacted private key]\n")
            continue
        if in_key:
            in_key = not (_KEY_END in text and "PRIVATE KEY-----" in text)
            continue
        if patch and text.startswith("GIT binary patch"):
            in_binary_patch = True
            target.write(b"Binary files differ\n")
            continue
        if in_binary_patch and not text.startswith("diff --git "):
            continue
        in_binary_patch = False
        target.write(clean_text(text).encode("utf-8", errors="surrogateescape"))
