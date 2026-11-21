"""The API's handle on Temporal: start workflows, send signals, cancel."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.config import Settings
from sca.core.errors import ConflictError
from sca.core.models import MessageMode
from sca.kernel.kind import get
from sca.observability import tracing_interceptors

REVIEW_SIGNAL = "submit_review"
PAUSE_SIGNAL = "pause"
RESUME_SIGNAL = "resume"
CI_SIGNAL = "ci_result"
USER_MESSAGE_SIGNAL = "user_message"


def workflow_id(task_id: str) -> str:
    return f"task-{task_id}"


class TemporalOrchestrator:
    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    @classmethod
    async def connect(cls, settings: Settings) -> TemporalOrchestrator:
        client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            interceptors=tracing_interceptors(),
        )
        return cls(client, settings.task_queue)

    async def start_task(self, task_id: str, kind: str) -> None:
        workflow_name = get(kind).workflow_name
        try:
            await self.client.start_workflow(
                workflow_name,
                str(task_id),
                id=workflow_id(str(task_id)),
                task_queue=self.task_queue,
            )
        except WorkflowAlreadyStartedError:
            pass  # a retried submit: the workflow id is derived from the task id

    async def _signal(self, task_id: str, signal: str, args: list) -> None:
        handle = self.client.get_workflow_handle(workflow_id(str(task_id)))
        try:
            await handle.signal(signal, args=args)
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                raise ConflictError("task is no longer running") from e
            raise

    async def submit_review(self, task_id: str, decision: str, feedback: str | None) -> None:
        await self._signal(task_id, REVIEW_SIGNAL, [decision, feedback])

    async def pause_task(self, task_id: str) -> None:
        await self._signal(task_id, PAUSE_SIGNAL, [])

    async def resume_task(self, task_id: str) -> None:
        await self._signal(task_id, RESUME_SIGNAL, [])

    async def submit_ci_result(
        self, task_id: str, commit: str, passed: bool, summary: str | None, url: str | None
    ) -> None:
        await self._signal(task_id, CI_SIGNAL, [commit, passed, summary, url])

    async def cancel_task(self, task_id: str) -> None:
        handle = self.client.get_workflow_handle(workflow_id(str(task_id)))
        try:
            await handle.cancel()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                raise ConflictError("task is no longer running") from e
            raise

    async def submit_task_message(self, task_id: str, message_id: str, mode: MessageMode) -> None:
        await self._signal(task_id, USER_MESSAGE_SIGNAL, [message_id, mode])
