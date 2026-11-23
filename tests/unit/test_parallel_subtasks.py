from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Replayer

from sca import config
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.workflow import CodingBeforeTurn


@pytest.mark.parametrize(('memory', 'cpus', 'budget', 'expected'), [
    (3800, 2, 700, 2), (3800, 4, 700, 3), (3800, 4, 1000, 2),
    (1024, 8, 700, 1), (65536, 16, 700, 8),
])
def test_auto_parallel_agents_reserves_host_memory(monkeypatch, memory, cpus, budget, expected):
    monkeypatch.setattr(config, '_host_memory_mb', lambda: memory)
    monkeypatch.setattr(config.os, 'cpu_count', lambda: cpus)
    config._auto_parallel_agents.cache_clear()
    try:
        assert config.Settings(_env_file=None, max_parallel_agents='auto', agent_memory_mb=budget
                               ).effective_max_parallel_agents == expected
    finally:
        config._auto_parallel_agents.cache_clear()


def test_parallel_agents_environment_override(monkeypatch):
    monkeypatch.setenv('SCA_MAX_PARALLEL_AGENTS', '3')
    monkeypatch.setenv('SCA_AGENT_MEMORY_MB', '500')
    settings = config.Settings(_env_file=None)
    assert settings.effective_max_parallel_agents == 3
    assert settings.agent_memory_mb == 500


@pytest.mark.parametrize('value', [0, 9, '2', True, 2.5])
def test_rejects_invalid_task_parallelism(value):
    with pytest.raises(ValidationError, match='max_parallel_agents'):
        CodingSpec(repo_url='repo', instruction='work', max_parallel_agents=value)


async def test_replay_before_bounded_subtasks_started_both_children_without_waiting():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    converter = DataConverter.default
    history: list[HistoryEvent] = []

    def event(name, **fields):
        result = HistoryEvent(event_id=len(history) + 1, event_type=EventType.Value('EVENT_TYPE_' + name.upper()))
        result.event_time.FromDatetime(at)
        attrs = getattr(result, name + '_event_attributes')
        attrs.CopyFrom(type(attrs)(**fields))
        history.append(result)
        return result.event_id

    def workflow_task():
        scheduled = event('workflow_task_scheduled', task_queue={'name': 'tasks'})
        started = event('workflow_task_started', scheduled_event_id=scheduled)
        return event('workflow_task_completed', scheduled_event_id=scheduled, started_event_id=started)

    async def activity_result(completed, index, name, result):
        scheduled = event('activity_task_scheduled', activity_type={'name': name},
                          activity_id=str(index), task_queue={'name': 'tasks'},
                          workflow_task_completed_event_id=completed)
        started = event('activity_task_started', scheduled_event_id=scheduled, attempt=1)
        event('activity_task_completed', scheduled_event_id=scheduled, started_event_id=started,
              result=await converter.encode_wrapper([result]))
        return workflow_task()

    spec = {'plan': 'none', 'turn_timeout_s': 60, '_max_parallel_agents': 1,
            'subtasks': [{'name': name, 'instruction': name} for name in ('one', 'two')]}
    event('workflow_execution_started', workflow_type={'name': 'coding.before_turn'},
          task_queue={'name': 'tasks'}, input=await converter.encode_wrapper(['legacy-task', spec]))
    completed = workflow_task()
    completed = await activity_result(completed, 1, 'coding.prepare_mirror', None)
    completed = await activity_result(completed, 2, 'set_state', None)
    children = []
    for name in ('one', 'two'):
        workflow_id = f'task-legacy-task-sub-{name}'
        child_type = {'name': 'CodingSubtaskWorkflow'}
        execution = {'workflow_id': workflow_id, 'run_id': f'run-{name}'}
        initiated = event('start_child_workflow_execution_initiated', workflow_id=workflow_id,
                          workflow_type=child_type, task_queue={'name': 'tasks'},
                          workflow_task_completed_event_id=completed)
        started = event('child_workflow_execution_started', workflow_type=child_type,
                        workflow_execution=execution, initiated_event_id=initiated)
        children.append((name, child_type, execution, initiated, started))
        completed = workflow_task()
    for name, child_type, execution, initiated, started in children:
        event('child_workflow_execution_completed', workflow_type=child_type, workflow_execution=execution,
              initiated_event_id=initiated, started_event_id=started,
              result=await converter.encode_wrapper([{'name': name, 'passed': True}]))
    completed = workflow_task()
    completed = await activity_result(completed, 3, 'coding.merge_subtasks', [])
    completed = await activity_result(completed, 4, 'coding.build_merged_workspace', None)
    event('workflow_execution_completed', workflow_task_completed_event_id=completed,
          result=await converter.encode_wrapper([{'skip_turn': True, 'unresolved': []}]))

    await Replayer(workflows=[CodingBeforeTurn]).replay_workflow(WorkflowHistory('legacy-task', history))
