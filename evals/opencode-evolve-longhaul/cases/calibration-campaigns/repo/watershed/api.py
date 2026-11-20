"""Small request/response interface for embedding the offline desk."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deskcore.application import Reply

from .runtime import APP


def request(
    workspace: Path,
    actor: str,
    method: str,
    route: str,
    params: Mapping[str, Any] | None = None,
    *,
    locale: str = "en",
) -> Reply:
    return APP.request(workspace, actor, method, route, params, locale=locale)
