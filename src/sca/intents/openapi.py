"""Parse OpenAPI locally and delegate contract execution to Schemathesis."""

import json
import re
import shlex
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sca.intents.base import Check, IntentBundle, IntentSource, source_bytes

DEFAULT_CHECKS = (
    "not_a_server_error",
    "status_code_conformance",
    "content_type_conformance",
    "response_schema_conformance",
)


class ServiceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_command: str = Field(min_length=1, max_length=8192)
    base_url: str = Field(pattern=r"^https?://", max_length=2048)
    ready_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2048)
    timeout_s: int = Field(default=30, ge=1, le=300)
    # What the contract means (design §C2.3): responses match the declared status codes,
    # content types and schemas, and the service does not fail. Schemathesis' `all` adds
    # stricter conventions (e.g. 405 for unlisted methods) a project can opt into here.
    checks: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CHECKS),
        min_length=1,
        max_length=20,
    )

    @field_validator("checks")
    @classmethod
    def _check_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(r"[a-z][a-z_]{0,63}", name):
                raise ValueError(f"invalid Schemathesis check name: {name!r}")
        return value


class OpenAPIAdapter:
    type = "openapi"

    def compile(self, source: IntentSource, workspace: Path) -> IntentBundle:
        params = ServiceParams.model_validate(source.params)
        data = source_bytes(source, workspace)
        try:
            spec = yaml.safe_load(data)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid OpenAPI YAML: {exc}") from exc
        if (
            not isinstance(spec, dict)
            or not isinstance(spec.get("openapi"), str)
            or not spec["openapi"].startswith("3.")
        ):
            raise ValueError("expected an OpenAPI 3 document")
        # Preserve the source verbatim; references are resolved only by the sandbox CLI.
        name = source.name + ".yaml"
        argv = [
            "schemathesis",
            "run",
            ".sca/context/" + name,
            "--url",
            params.base_url,
            "--checks",
            ",".join(params.checks),
        ]
        config = source.name + "-service.json"
        runner = Path(__file__).with_name("service_runner.py").read_bytes()
        return IntentBundle(
            context_files={
                name: data,
                "service_runner.py": runner,
                config: json.dumps({**params.model_dump(), "argv": argv}).encode(),
            },
            checks=[
                Check(
                    name=source.name,
                    command=shlex.join(
                        ["python", ".sca/context/service_runner.py", ".sca/context/" + config]
                    ),
                )
            ],
        )
