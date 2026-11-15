"""Versioned scenario templates, rendered only in activities."""

from pathlib import Path
from string import Template

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EvaluationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCA_", env_file=".env", extra="ignore")
    default_prompt_version: str = Field(default="v1", pattern=r"^[a-zA-Z0-9_-]+$")


def render_prompt(spec: dict, name: str, **values: object) -> str:
    kind = spec.get("_kind", "analysis" if "question" in spec else "coding")
    version = spec.get("prompt_version") or EvaluationSettings().default_prompt_version
    if kind not in {"coding", "analysis"} or not version.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid prompt selection")
    context = {**spec, **values}
    if kind == "coding":
        commands = [c for c in [spec.get("verify_command"), *spec.get("verify_commands", [])] if c]
        mode = "single" if len(commands) == 1 else "multiple" if commands else "none"
        paragraph = Path(__file__).parents[1] / "kinds" / kind / "prompts" / version / f"verify_{mode}.txt"
        context["verification"] = Template(paragraph.read_text()).substitute(
            command=commands[0] if commands else "",
            listing="\n".join(f"    {command}" for command in commands),
        )
        context["instruction"] = spec.get("instruction", "").strip() if name == "initial" else spec.get("instruction", "")
        context["verify_command"] = " && ".join(commands)
        if "output" in context:
            context["output"] = str(context["output"]).strip()
        if "feedback" in context:
            context["feedback"] = str(context["feedback"]).strip()
    else:
        context.setdefault("checks", [])
        context.setdefault("report_format", "markdown")
    path = Path(__file__).parents[1] / "kinds" / kind / "prompts" / version / f"{name}.txt"
    return Template(path.read_text()).substitute(context)
