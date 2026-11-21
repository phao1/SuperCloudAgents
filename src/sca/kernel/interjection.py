"""Deterministic turn-boundary handling of task guidance."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, is_cancelled_exception

from sca.core.models import MessageMode

USER_MESSAGE_SIGNAL = "user_message"
MESSAGES_PENDING = "messages_pending"
HEARTBEAT_INTERVAL = timedelta(seconds=2)
_QUICK = {"start_to_close_timeout": timedelta(seconds=30)}
_GUIDANCE = "User guidance received during the run (highest priority, may change direction):"
CONTINUE_WITH_GUIDANCE = "Continue from the current workspace and address the user guidance."


@dataclass
class Guidance:
    id: str
    text: str


@dataclass
class DeliverMessages:
    task_id: str
    turn: int
    planning: bool = False


@dataclass
class ApplyMessages:
    task_id: str
    message_ids: list[str]


def with_guidance(prompt: str, messages: list[str]) -> str:
    if not messages:
        return prompt
    return "\n\n".join([_GUIDANCE, *messages, prompt])


class TurnInterrupted(Exception):
    """An interrupt requested by a task message, distinct from workflow cancellation."""


class Interjections:
    def __init__(self) -> None:
        self.enabled = False
        self._notices: dict[str, MessageMode] = {}
        self._known: set[str] = set()
        self._read_requested = False
        self._active: workflow.ActivityHandle | None = None
        self._interrupted = False
        self.applied_ids: list[str] = []

    def enable(self) -> bool:
        self.enabled = workflow.patched("user-interjection")
        return self.enabled

    @property
    def pending(self) -> bool:
        return self._read_requested or bool(self._notices)

    def request_delivery(self) -> None:
        self._read_requested = True

    def notify(self, message_id: str, mode: MessageMode) -> bool:
        if not self.enable() or message_id in self._known or message_id in self._notices:
            return False
        self._notices[message_id] = mode
        if mode == "interrupt" and self._active and not self._active.done() and not self._interrupted:
            self._interrupted = self._active.cancel()
        return True

    def notices(self) -> list[tuple[str, MessageMode]]:
        return list(self._notices.items())

    def acknowledge(self, message_ids: list[str]) -> None:
        self._known.update(message_ids)
        for message_id in message_ids:
            self._notices.pop(message_id, None)

    async def deliver(self, task_id: str, turn: int, *, planning: bool = False) -> list[Guidance]:
        if not self.enabled:
            return []
        while True:
            notified = list(self._notices)
            self._read_requested = False
            messages = await workflow.execute_activity(
                "deliver_task_messages", DeliverMessages(task_id, turn, planning),
                result_type=list[Guidance], **_QUICK,
            )
            # Read in database order, even when concurrent HTTP requests signal out of order.
            self.acknowledge([*notified, *(message.id for message in messages)])
            if not self.pending:
                return messages

    async def apply(self, task_id: str, messages: list[Guidance]) -> None:
        if messages:
            ids = [message.id for message in messages]
            await workflow.execute_activity("apply_task_messages", ApplyMessages(task_id, ids), **_QUICK)
            self.applied_ids.extend(ids)

    async def run_activity(self, name: str, arg: Any, **options: Any) -> Any:
        if not self.enabled:
            return await workflow.execute_activity(name, arg, **options)
        handle = workflow.start_activity(
            name, arg, cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            **options,
        )
        self._active = handle
        self._interrupted = False
        try:
            return await handle
        except (ActivityError, asyncio.CancelledError) as exc:
            current = asyncio.current_task()
            if self._interrupted and current and not current.cancelling() and is_cancelled_exception(exc):
                self.request_delivery()
                raise TurnInterrupted from exc
            raise
        finally:
            self._active = None
