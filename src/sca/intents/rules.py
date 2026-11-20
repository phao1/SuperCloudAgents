"""Compile linter configuration into a sandbox command."""

import json
import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sca.intents.base import Check, IntentBundle, IntentSource, source_bytes


class RulesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["ruff", "eslint", "semgrep"]
    description: str = ""
    eslint_format: Literal["json", "js", "mjs", "cjs"] = "mjs"


class RulesAdapter:
    type = "rules"

    def compile(self, source: IntentSource, workspace: Path) -> IntentBundle:
        params = RulesParams.model_validate(source.params)
        suffix = {"ruff": ".toml", "eslint": "." + params.eslint_format, "semgrep": ".yaml"}[
            params.tool
        ]
        name = source.name + suffix
        path = ".sca/context/" + name
        argv = {
            "ruff": ["python", "-m", "ruff", "check", "--config", path, "."],
            "eslint": ["eslint", "--config", path, "."],
            "semgrep": ["semgrep", "scan", "--config", path, "."],
        }[params.tool]
        files = {name: source_bytes(source, workspace)}
        if params.description:
            files[source.name + ".md"] = params.description.encode()
        return IntentBundle(
            context_files=files,
            checks=[Check(name=source.name, command=shlex.join(argv))],
            setup=[
                {
                    "ruff": "python -m pip install ruff",
                    "semgrep": "python -m pip install semgrep",
                    "eslint": "npm install --global eslint",
                }[params.tool]
            ],
        )


def design_token_check(name: str, literals: dict[str, list[str]]) -> IntentBundle:
    if not literals:
        return IntentBundle()
    config = name + "-token-literals.json"
    command = shlex.join(["python", ".sca/context/token_check.py", ".sca/context/" + config])
    return IntentBundle(
        context_files={
            "token_check.py": Path(__file__).with_name("token_check.py").read_bytes(),
            config: json.dumps(literals).encode(),
        },
        checks=[Check(name=name, command=command + ' "$1"', needs_base_sha=True)],
    )
