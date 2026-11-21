from datetime import UTC, datetime

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Replayer

from sca.kinds.coding.kind import workflow_spec
from sca.kinds.coding.workflow import CodingTaskWorkflow


async def test_replay_pre_interjection_history_without_a_server():
    """Replay a completed coding task whose activity payloads predate interjection."""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    converter = DataConverter.default
    history: list[HistoryEvent] = []

    def event(name, **fields):
        result = HistoryEvent(event_id=len(history) + 1, event_type=EventType.Value("EVENT_TYPE_" + name.upper()))
        result.event_time.FromDatetime(at)
        attrs = getattr(result, name + "_event_attributes")
        attrs.CopyFrom(type(attrs)(**fields))
        history.append(result)
        return result.event_id

    def workflow_task():
        scheduled = event("workflow_task_scheduled", task_queue={"name": "tasks"})
        started = event("workflow_task_started", scheduled_event_id=scheduled)
        return event("workflow_task_completed", scheduled_event_id=scheduled, started_event_id=started)

    event(
        "workflow_execution_started", workflow_type={"name": "CodingTaskWorkflow"},
        task_queue={"name": "tasks"}, input=await converter.encode_wrapper(["legacy-task"]),
    )
    completed = workflow_task()
    activities = (
        ("load_task", workflow_spec({
            "instruction": "Legacy task", "max_fix_attempts": 0, "turn_timeout_s": 60,
            "auto_finish": True,
        })),
        ("set_state", None),
        ("coding.prepare_workspace", {"base_sha": "abc", "branch": "sca/abc"}),
        ("create_sandbox", "sandbox"),
        ("coding.initial_prompt", "Legacy task"),
        ("check_budget", None),
        ("set_state", None),
        ("run_agent_turn", {"session_id": "session", "final_message": "finished"}),
        ("coding.checkpoint_workspace", {"commit": "abc", "changed": True}),
        ("check_budget", None),
        ("set_state", None),
        ("coding.run_verification", {"passed": True, "exit_code": 0, "output_tail": ""}),
        ("coding.publish", None),
        ("set_state", None),
        ("set_state", None),
        ("destroy_sandbox", None),
    )
    for index, (activity_type, result) in enumerate(activities, start=1):
        scheduled = event(
            "activity_task_scheduled", activity_type={"name": activity_type},
            activity_id=str(index),
            task_queue={"name": "tasks"}, workflow_task_completed_event_id=completed,
        )
        started = event("activity_task_started", scheduled_event_id=scheduled, attempt=1)
        event("activity_task_completed", scheduled_event_id=scheduled, started_event_id=started,
              result=await converter.encode_wrapper([result]))
        completed = workflow_task()
    event("workflow_execution_completed", workflow_task_completed_event_id=completed,
          result=await converter.encode_wrapper(["SUCCEEDED"]))

    await Replayer(workflows=[CodingTaskWorkflow]).replay_workflow(WorkflowHistory("legacy-task", history))
