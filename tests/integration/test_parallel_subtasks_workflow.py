"""Run bounded DAGs with scripted activities on Temporal and in process."""

import asyncio
import socket
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from sca.kernel.interjection import (
    HEARTBEAT_INTERVAL,
    USER_MESSAGE_SIGNAL,
    ApplyMessages,
    DeliverMessages,
    Guidance,
)
from sca.kinds.coding.activities import SetStateInput
from sca.kinds.coding.best_of import (
    Attempt,
    AttemptStep,
    Candidate,
    DiffStats,
    RecordAttempt,
    SelectAttempt,
    SubtaskAttempts,
)
from sca.kinds.coding.kind import prompt_activities
from sca.kinds.coding.subtask_inputs import (
    MergeInput,
    SubtaskInput,
    SubtaskPreparationInput,
    SubtaskProgress,
    SubtaskSchedule,
)
from sca.kinds.coding.workflow import CodingBeforeTurn, CodingSubtaskWorkflow

pytestmark = pytest.mark.integration


class Script:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.peak = 0
        self.turns: asyncio.Queue[SubtaskInput] = asyncio.Queue()
        self.queued: asyncio.Queue[str] = asyncio.Queue()
        self.finished: set[str] = set()
        self.release = asyncio.Event()
        self.messages: dict[str, Guidance] = {}
        self.applied: set[str] = set()
        self.interrupted: set[str] = set()

    def start(self, name: str) -> None:
        self.active.add(name)
        self.peak = max(self.peak, len(self.active))

    @activity.defn(name='set_state')
    async def state(self, inp: SetStateInput) -> None:
        pass

    @activity.defn(name='coding.prepare_mirror')
    async def mirror(self, task_id: str) -> None:
        pass

    @activity.defn(name='coding.prepare_subtask_base')
    async def base(self, task_id: str) -> None:
        pass

    @activity.defn(name='coding.prepare_scoped_subtask')
    async def prepare(self, inp: SubtaskPreparationInput) -> dict:
        assert {result.name for result in inp.dependencies} <= self.finished
        self.start(inp.name)
        return {}

    @activity.defn(name='coding.subtask_step')
    async def step(self, inp: SubtaskInput) -> dict:
        match inp.action:
            case 'prepare':
                self.start(inp.name)
            case 'run_agent_turn':
                self.turns.put_nowait(inp)
                try:
                    while not self.release.is_set() and inp.turn == 1:
                        activity.heartbeat()
                        with suppress(TimeoutError):
                            await asyncio.wait_for(self.release.wait(), .1)
                except asyncio.CancelledError:
                    self.interrupted.add(inp.name)
                    raise
                return {'session_id': 'fake-session'}
            case 'checkpoint_workspace':
                return {'commit': inp.name, 'changed': True}
            case 'run_verification':
                self.finished.add(inp.name)
                return {'passed': True}
            case 'destroy_sandbox':
                self.active.discard(inp.name)
        return {}

    @activity.defn(name='coding.best_of_step')
    async def attempt_step(self, inp: AttemptStep) -> dict:
        return await self.step(inp.step)

    @activity.defn(name='coding.best_of_subtask_start')
    async def start_attempts(self, inp: SubtaskAttempts) -> list[Attempt]:
        self.active.remove(inp.name)
        return [Attempt(number, 'fake', None, None, None) for number in range(1, inp.n + 1)]

    @activity.defn(name='coding.best_of_record')
    async def record_attempt(self, inp: RecordAttempt) -> Candidate:
        return Candidate(inp.attempt.attempt, 'fake', None, inp.result.commit,
                         {'passed': inp.result.passed}, None, 1, DiffStats(1, 1, 0), 'fixture.patch')

    @activity.defn(name='coding.best_of_adopt')
    async def adopt(self, inp: SelectAttempt) -> None:
        self.finished.add(inp.subtask)

    @activity.defn(name='coding.best_of_subtask_cleanup')
    async def cleanup(self, inp: SubtaskAttempts) -> None:
        pass

    @activity.defn(name='coding.record_subtask_schedule')
    async def schedule(self, inp: SubtaskSchedule) -> None:
        assert inp.max_parallel_agents == 2

    @activity.defn(name='coding.record_subtask_progress')
    async def progress(self, inp: SubtaskProgress) -> None:
        if inp.stage == 'queued':
            self.queued.put_nowait(inp.name)

    @activity.defn(name='coding.merge_subtasks')
    async def merge(self, inp: MergeInput) -> list[dict]:
        assert not self.active
        assert all(result.passed for result in inp.results)
        return [{'name': result.name, 'status': 'merged'} for result in inp.results]

    @activity.defn(name='coding.build_merged_workspace')
    async def build(self, task_id: str) -> None:
        pass

    @activity.defn(name='deliver_task_messages')
    async def deliver(self, inp: DeliverMessages) -> list[Guidance]:
        ids = inp.message_ids if inp.message_ids is not None else self.messages.keys() - self.applied
        return [self.messages[id_] for id_ in ids]

    @activity.defn(name='apply_task_messages')
    async def apply(self, inp: ApplyMessages) -> None:
        self.applied.update(inp.message_ids)

    def activities(self):
        return [self.state, self.mirror, self.base, self.prepare, self.step, self.attempt_step,
                self.start_attempts, self.record_attempt, self.adopt, self.cleanup, self.schedule, self.progress,
                self.merge, self.build, self.deliver, self.apply, *prompt_activities()]


class LocalChild(asyncio.Task):
    def __init__(self, instance, args, id_):
        self.instance = instance
        self.id = id_
        super().__init__(instance.run(*args))

    async def signal(self, name, *, args):
        assert name == USER_MESSAGE_SIGNAL
        await self.instance.user_message(*args)


@pytest.fixture(params=['local', 'temporal'])
async def runtime(request, monkeypatch, tmp_path):
    if request.param == 'local':
        monkeypatch.setattr(workflow, 'patched', lambda _: True)

        @asynccontextmanager
        async def start(script, spec):
            definitions = {activity._Definition.must_from_callable(fn).name: fn for fn in script.activities()}

            async def execute(name, *positional, args=None, **options):
                return await ActivityEnvironment().run(definitions[name], *(args if args is not None else positional))

            async def child(entry, *, args, id, **options):
                assert entry == CodingSubtaskWorkflow.run
                return LocalChild(CodingSubtaskWorkflow(), args, id)

            monkeypatch.setattr(workflow, 'execute_activity', execute)
            monkeypatch.setattr(workflow, 'start_activity', lambda *args, **kw: asyncio.create_task(execute(*args, **kw)))
            monkeypatch.setattr(workflow, 'start_child_workflow', child)
            parent = LocalChild(CodingBeforeTurn(), [str(uuid4()), spec], 'local-root')
            try:
                yield parent, parent, None
            finally:
                if not parent.done():
                    parent.cancel()
                await asyncio.gather(parent, return_exceptions=True)

        yield start
        return

    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
    except OSError:
        pytest.skip('Temporal test environment requires a loopback socket')
    try:
        async with asyncio.timeout(25):
            env = await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(tmp_path))
    except (RuntimeError, OSError, TimeoutError):
        pytest.skip('Temporal test server is unavailable')

    @asynccontextmanager
    async def start(script, spec):
        queue = str(uuid4())
        workflows = [CodingBeforeTurn, CodingSubtaskWorkflow]
        async with Worker(env.client, task_queue=queue, workflows=workflows, activities=script.activities(),
                          max_heartbeat_throttle_interval=HEARTBEAT_INTERVAL):
            handle = await env.client.start_workflow(CodingBeforeTurn.run, args=[str(uuid4()), spec],
                                                     id=f'parallel-{queue}', task_queue=queue)
            yield handle, handle.result(), Replayer(workflows=workflows)

    async with env:
        yield start


def spec(*subtasks):
    return {'instruction': 'Do the work', 'plan': 'none', 'subtasks': list(subtasks), 'turn_timeout_s': 60,
            'max_fix_attempts': 0, '_max_parallel_agents': 2, '_user_interjection': True}


def subtask(name, **options):
    return {'name': name, 'instruction': name, **options}


@pytest.mark.parametrize('mode', ['queue', 'interrupt'])
async def test_cap_dependencies_and_interjection_fanout(runtime, mode):
    script = Script()
    task = spec(subtask('api'), subtask('ui'), subtask('docs'), subtask('tests', depends_on=['api', 'ui']))
    async with runtime(script, task) as (root, result, replayer), asyncio.timeout(15):
        first_two = {(await script.turns.get()).name, (await script.turns.get()).name}
        assert len(first_two) == 2 and 'tests' not in first_two
        waiting = await script.queued.get()
        assert {waiting, *first_two} == {'api', 'ui', 'docs'}
        assert script.turns.empty()

        script.messages['guide'] = Guidance('guide', 'Cover an empty input')
        await root.signal(USER_MESSAGE_SIGNAL, args=['guide', mode])
        if mode == 'queue':
            if replayer:
                while sum(event.HasField('external_workflow_execution_signaled_event_attributes')
                          for event in (await root.fetch_history()).events) < 2:
                    await asyncio.sleep(.05)
            script.release.set()
        guided = set()
        while not first_two <= guided:
            turn = await script.turns.get()
            if turn.turn == 2 and 'Cover an empty input' in turn.prompt:
                guided.add(turn.name)
        script.release.set()
        assert await result == {'skip_turn': True, 'unresolved': [], 'applied_messages': ['guide']}
        if replayer:
            await replayer.replay_workflow(await root.fetch_history())
    assert script.peak == 2
    assert script.interrupted == (first_two if mode == 'interrupt' else set())


async def test_nested_best_of_uses_the_same_capacity(runtime):
    script = Script()
    task = spec(subtask('batch', best_of={'n': 3}), subtask('other'), subtask('join', depends_on=['batch']))
    async with runtime(script, task) as (_, result, _), asyncio.timeout(15):
        assert await script.queued.get() == 'other'
        assert {(await script.turns.get()).name, (await script.turns.get()).name} == {'best-of-1', 'best-of-2'}
        assert script.turns.empty()
        script.release.set()
        assert await result == {'skip_turn': True, 'unresolved': []}
    assert script.finished == {'batch', 'other', 'join', 'best-of-1', 'best-of-2', 'best-of-3'}
    assert script.peak == 2
