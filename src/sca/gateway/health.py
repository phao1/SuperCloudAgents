"""Share the latest model outcome between the local worker and Portal API."""

from __future__ import annotations

import fcntl
import json
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sca.core.model_provider import ModelProviderError, valid_provider_kind

log = logging.getLogger(__name__)


class ProviderHealth:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "run" / "model-provider-health.json"

    def read(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text())
            result = {}
            for field in ("last_error", "account_error"):
                entry = data.get(field)
                if isinstance(entry, dict) and valid_provider_kind(entry.get("kind")):
                    at = datetime.fromisoformat(entry["at"])
                    result[field] = {"kind": entry["kind"], "at": at.isoformat()}
            return result
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            return {}

    def record(self, error: ModelProviderError | None) -> None:
        """Publish an observation from a model call. A success clears the account alert."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                # The worker and API can both make model calls.
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._write(error)
        except OSError:
            log.warning("Could not update model provider health")

    def _write(self, error: ModelProviderError | None) -> None:
        if error is None:
            self.path.unlink(missing_ok=True)
            return
        status = self.read()
        entry = {"kind": error.kind, "at": datetime.now(UTC).isoformat()}
        status["last_error"] = entry
        if error.is_account_error:
            status["account_error"] = entry
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(status, stream)
            temporary.replace(self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
