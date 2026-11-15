"""Analysis kind registration and prompt activities."""

from temporalio import activity

from sca.kernel.kind import KindDeps, KindHooks
from sca.kinds.analysis import prompts
from sca.kinds.analysis.spec import AnalysisSpec


class AnalysisKind:
    name = "analysis"
    spec_model = AnalysisSpec
    workflow_name = "AnalysisTaskWorkflow"
    hooks = KindHooks(
        prepare="analysis.prepare",
        checkpoint="analysis.checkpoint",
        verify="analysis.verify",
        publish="analysis.publish",
        initial_prompt="analysis.initial_prompt",
        fix_prompt="analysis.fix_prompt",
        feedback_prompt="analysis.feedback_prompt",
    )

    def workflow_options(self, spec: dict) -> dict:
        return {}

    def workflows(self) -> list:
        from sca.kinds.analysis.workflow import AnalysisTaskWorkflow

        return [AnalysisTaskWorkflow]

    def activities(self, deps: KindDeps) -> list:
        from sca.kinds.analysis.activities import AnalysisActivities

        acts = AnalysisActivities(**{f: getattr(deps, f) for f in deps.__dataclass_fields__})
        return acts.all() + [initial_prompt, fix_prompt, feedback_prompt]


@activity.defn(name="analysis.initial_prompt")
async def initial_prompt(spec: dict) -> str:
    return prompts.initial_prompt(spec)


@activity.defn(name="analysis.fix_prompt")
async def fix_prompt(spec: dict, exit_code: int, output_tail: str) -> str:
    return prompts.fix_prompt(exit_code, output_tail)


@activity.defn(name="analysis.feedback_prompt")
async def feedback_prompt(feedback: str, unresolved: list[dict]) -> str:
    return prompts.feedback_prompt(feedback)
