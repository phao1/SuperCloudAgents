"""Coding scenario registration."""

from dataclasses import asdict

from temporalio import activity

from sca.kernel.kind import KindDeps, KindHooks
from sca.kinds.coding import prompts
from sca.kinds.coding.spec import CodingSpec


class CodingKind:
    name = "coding"
    spec_model = CodingSpec
    workflow_name = "CodingTaskWorkflow"
    hooks = KindHooks(
        prepare="coding.prepare_workspace",
        checkpoint="coding.checkpoint_workspace",
        verify="coding.run_verification",
        publish="coding.publish",
        initial_prompt="coding.initial_prompt",
        fix_prompt="coding.fix_prompt",
        feedback_prompt="coding.feedback_prompt",
        extensions=["coding.before_turn", "coding.after_verify"],
    )

    def workflow_options(self, spec: dict) -> dict:
        return {
            "extensions_before": bool(spec.get("subtasks")) or spec.get("plan") == "auto",
            "extensions_after": spec.get("ci") is not None,
        }

    def workflows(self) -> list:
        from sca.kinds.coding.workflow import (
            CodingAfterVerify,
            CodingBeforeTurn,
            CodingSubtaskWorkflow,
            CodingTaskWorkflow,
        )

        return [CodingTaskWorkflow, CodingSubtaskWorkflow, CodingBeforeTurn, CodingAfterVerify]

    def activities(self, deps: KindDeps) -> list:
        from sca.kinds.coding.activities import CodingActivities
        from sca.kinds.coding.planning import PlanningActivities

        acts = CodingActivities(**{f: getattr(deps, f) for f in deps.__dataclass_fields__})
        return acts.all() + PlanningActivities(acts).all() + prompt_activities()


def workflow_spec(spec: dict) -> dict:
    return {
        **spec,
        "hooks": asdict(CodingKind.hooks),
        **CodingKind().workflow_options(spec),
    }


def checks(spec: dict) -> list[str]:
    return [c for c in [spec.get("verify_command"), *spec.get("verify_commands", [])] if c]


@activity.defn(name="coding.initial_prompt")
async def initial_prompt(spec: dict) -> str:
    return prompts.initial_prompt(spec["instruction"], checks(spec))


@activity.defn(name="coding.fix_prompt")
async def fix_prompt(spec: dict, exit_code: int, output_tail: str) -> str:
    return prompts.fix_prompt(" && ".join(checks(spec)), exit_code, output_tail)


@activity.defn(name="coding.feedback_prompt")
async def feedback_prompt(feedback: str, unresolved: list[dict]) -> str:
    details = "\n".join(
        f"- {r['name']} ({r['status']}): /workspace/.sca/subtask-{r['name']}.patch"
        for r in unresolved
    )
    if details:
        feedback += "\nUnmerged subtasks; resolve their patches:\n" + details
    return prompts.feedback_prompt(feedback)


@activity.defn(name="coding.ci_fix_prompt")
async def ci_fix_prompt(summary: str, url: str | None) -> str:
    return prompts.ci_fix_prompt(summary, url)


def prompt_activities() -> list:
    return [initial_prompt, fix_prompt, feedback_prompt, ci_fix_prompt]
