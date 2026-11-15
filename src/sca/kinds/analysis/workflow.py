"""Analysis uses the common task lifecycle."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sca.kernel.workflow import TaskWorkflow


@workflow.defn(name="AnalysisTaskWorkflow")
class AnalysisTaskWorkflow(TaskWorkflow):
    @workflow.run
    async def run(self, task_id: str) -> str:
        return await super().run(task_id)
