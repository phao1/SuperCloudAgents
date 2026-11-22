"""Real Temporal orchestration with fake eval children and I/O activities."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

# The sandbox re-imports this module to validate its workflows; httpx is restricted there.
with workflow.unsafe.imports_passed_through():
    import httpx

from sca.config import Settings, get_settings
from sca.eval.activities import EvalActivities
from sca.eval.suites import load_suite
from sca.eval.workflow import EvalRunWorkflow
from sca.evolve.activities import EvolutionActivities
from sca.evolve.models import EvolutionConfig
from sca.evolve.workflow import EvolutionWorkflow

pytestmark = pytest.mark.integration


@workflow.defn(name='EvalRunWorkflow')
class FakeEval:
    @workflow.run
    async def run(self, request: dict) -> str:
        return 'completed'


@workflow.defn(name='EvalRunWorkflow')
class SteerableFakeEval:
    def __init__(self) -> None:
        self.skipped = False

    @workflow.signal
    def skip_evolution_candidate(self) -> None:
        self.skipped = True

    @workflow.run
    async def run(self, request: dict) -> str:
        if not request['block']:
            return 'completed'
        await workflow.execute_activity('evo_test_candidate_ready', start_to_close_timeout=timedelta(seconds=5))
        await workflow.wait_condition(lambda: self.skipped)
        return 'cancelled'


@pytest.fixture
async def temporal_env():
    try:
        async with asyncio.timeout(3):
            client = await Client.connect(get_settings().temporal_address)
    except (RuntimeError, OSError, TimeoutError) as exc:
        pytest.skip(f'Temporal unavailable: {exc}')
    async with WorkflowEnvironment.from_client(client) as env:
        yield env


@pytest.mark.parametrize('scenario,expected,promotions', [
    ('promotion', 'completed', 1),
    ('rejection', 'completed', 0),
    ('budget', 'budget_exhausted', 0),
    ('patience', 'patience_exhausted', 0),
    ('approval', 'completed', 1),
    ('reject_approval', 'completed', 0),
    ('stop', 'stopped', 0),
])
async def test_evolution_paths(temporal_env, scenario, expected, promotions):
    runs, records, statuses, promoted = {}, {}, [], []
    waiting = asyncio.Event()
    cfg = EvolutionConfig(suite='test', agent_version='v', generations=3 if scenario == 'patience' else 1,
                          candidates=1, repeat=1, patience=2,
                          budget_usd=.1 if scenario == 'budget' else 10,
                          require_approval=scenario in ('approval', 'reject_approval', 'stop')).model_dump()

    @activity.defn(name='evolve_initialize')
    async def initialize(request: dict) -> dict:
        return {'id': None, 'candidate_id': None, 'overlay': {}}

    @activity.defn(name='evolve_prepare_run')
    async def prepare(request: dict) -> dict:
        runs[request['run_id']] = request
        return {'id': request['run_id']}

    @activity.defn(name='evolve_read_run')
    async def read(request: dict) -> dict:
        item = runs[request['run_id']]
        passed = bool(item['overlay'])
        if scenario in ('rejection', 'patience') and item['split'] == 'holdout':
            passed = not bool(item['overlay'])
        return {'id': request['run_id'], 'status': 'completed', 'suite_hash': 'same',
                'results': [{'case_id': 'c', 'repeat': 0, 'acceptance_passed': passed,
                             'verification_passed': True, 'trajectory_passed': True,
                             'cost_usd': .1, 'turns': 1}]}

    @activity.defn(name='evolve_propose')
    async def propose(request: dict) -> list[dict]:
        return [{'hypothesis': 'verify', 'overlay': {'tool_guidance': 'verify'},
                 'overlay_sha256': 'hash', 'diagnosis': {}, 'static_check': {'valid': True}}]

    @activity.defn(name='evolve_record')
    async def record(request: dict) -> None:
        records[request['candidate']['id']] = request['candidate']

    @activity.defn(name='evolve_status')
    async def status(request: dict) -> None:
        statuses.append(request)
        if request['status'] == 'awaiting_approval':
            waiting.set()

    @activity.defn(name='evolve_promote')
    async def promote(request: dict) -> dict:
        promoted.append(request)
        return {'id': request['champion_id'], 'candidate_id': request['candidate']['id'],
                'overlay': request['candidate']['overlay']}

    @activity.defn(name='evolve_conversation')
    async def conversation(request: dict) -> None:
        pass

    queue = f'evolve-test-{uuid4()}'
    async with Worker(temporal_env.client, task_queue=queue, workflows=[EvolutionWorkflow, FakeEval],
                      activities=[initialize, prepare, read, propose, record, status, promote, conversation]):
        async with asyncio.timeout(45):
            handle = await temporal_env.client.start_workflow(EvolutionWorkflow.run,
                {'id': str(uuid4()), 'config': cfg}, id=f'evolve-{uuid4()}', task_queue=queue)
            if cfg['require_approval']:
                await waiting.wait()
                pending = next(c for c in records.values() if c['reason'] == 'awaiting approval')
                await handle.signal('approve', 'stale-candidate')
                if scenario == 'stop':
                    await handle.signal('stop')
                else:
                    await handle.signal('reject' if scenario == 'reject_approval' else 'approve', pending['id'])
            assert await handle.result() == expected
    assert len(promoted) == promotions
    assert statuses[-1]['status'] == expected
    assert statuses[-1]['spent_usd'] <= cfg['budget_usd']
    if scenario == 'patience':
        assert len(records) == 2
        assert sum(r['split'] == 'dev' and not r['overlay'] for r in runs.values()) == 1
    assert all(c['decision'] != 'pending' for c in records.values())


async def test_live_steering_and_replay(temporal_env):
    started, release, paused, evaluating = (asyncio.Event() for _ in range(4))
    runs, records, transcript, proposals = {}, {}, [], []

    @activity.defn(name='evolve_initialize')
    async def initialize(request: dict) -> dict:
        started.set()
        await release.wait()
        return {'id': None, 'candidate_id': None, 'overlay': {}}

    @activity.defn(name='evolve_prepare_run')
    async def prepare(request: dict) -> dict:
        run_id = request['run_id']
        runs[run_id] = {'block': len(runs) == 1, 'id': run_id}
        return runs[run_id]

    @activity.defn(name='evolve_read_run')
    async def read(request: dict) -> dict:
        run = runs[request['run_id']]
        return {'id': run['id'], 'status': 'cancelled' if run['block'] else 'completed', 'suite_hash': 'same',
                'results': [{'case_id': 'a', 'repeat': 0, 'acceptance_passed': True,
                             'cost_usd': .002, 'turns': 1}]}

    @activity.defn(name='evolve_propose')
    async def propose(request: dict) -> list[dict]:
        proposals.append(request['operator_guidance'])
        return [{'hypothesis': 'Check boundaries', 'overlay': {'tool_guidance': 'Be precise'},
                 'static_check': {'valid': True}}]

    @activity.defn(name='evolve_record')
    async def record(request: dict) -> None:
        records[request['candidate']['id']] = request['candidate']

    @activity.defn(name='evolve_status')
    async def status(request: dict) -> None:
        pass

    @activity.defn(name='evolve_conversation')
    async def conversation(request: dict) -> None:
        transcript.extend(request['items'])
        if request.get('evolution_status', {}).get('status') == 'paused':
            paused.set()

    @activity.defn(name='evo_test_candidate_ready')
    async def candidate_ready() -> None:
        evaluating.set()

    config = EvolutionConfig(suite='fixture', agent_backend='dsh', generations=2, candidates=1).model_dump()
    workflows = [EvolutionWorkflow, SteerableFakeEval]
    queue = f'evochat-test-{uuid4()}'
    async with Worker(temporal_env.client, task_queue=queue, workflows=workflows,
                      activities=[initialize, prepare, read, propose, record, status, conversation, candidate_ready]):
        async with asyncio.timeout(30):
            handle = await temporal_env.client.start_workflow(
                EvolutionWorkflow.run, {'id': str(uuid4()), 'config': config}, id=f'evolve-{uuid4()}', task_queue=queue,
            )
            await started.wait()
            await handle.signal(EvolutionWorkflow.user_message, {'id': 'guidance', 'mode': 'guidance', 'text': 'Check bounds'})
            await handle.signal(EvolutionWorkflow.user_message, {'id': 'pause', 'mode': 'pause'})
            release.set()
            await paused.wait()
            assert not runs
            await handle.signal(EvolutionWorkflow.user_message, {'id': 'resume', 'mode': 'resume'})
            await evaluating.wait()
            await handle.signal(EvolutionWorkflow.user_message, {'id': 'stop', 'mode': 'stop'})
            await handle.signal(EvolutionWorkflow.user_message, {'id': 'skip', 'mode': 'skip'})
            assert await handle.result() == 'stopped'
    assert proposals == ['Check bounds']
    assert [row['reason'] for row in records.values()] == ['操作者已跳过候选']
    assert {item['message_id'] for item in transcript if item['kind'] == 'receipt'} == {
        'guidance', 'pause', 'resume', 'stop', 'skip',
    }
    await Replayer(workflows=workflows).replay_workflow(await handle.fetch_history())


async def test_suite_change_fails_eval_and_evolution(temporal_env, store, tmp_path, monkeypatch):
    suite_path = tmp_path / 'suite'
    for case_id in ('a', 'b', 'c'):
        case = suite_path / 'cases' / case_id
        (case / 'data').mkdir(parents=True)
        (case / 'data/input.csv').write_text('value\n1\n')
        (case / 'acceptance').mkdir()
        (case / 'acceptance/hidden.py').write_text('assert True\n')
        (case / 'acceptance.yaml').write_text('command: python hidden.py\n')
        (case / 'task.yaml').write_text('split: dev\nquestion: Count rows\n')
    (suite_path / 'suite.yaml').write_text('name: test\nkind: analysis\n')
    config = EvolutionConfig(suite=str(suite_path), agent_version='v', repeat=1, concurrency=1,
                             generations=1).model_dump()
    evolution_id = uuid4()
    scoped = store.for_tenant('eval')
    await scoped.create_evolution({'id': evolution_id, 'status': 'queued', 'config': config,
                                  'created_by': 'test'})

    settings = Settings(_env_file=None, data_dir=tmp_path / 'state')
    evaluation = EvalActivities(settings, store)
    evolution = EvolutionActivities(settings, store, None)
    task_id = str(uuid4())
    submissions, attempts = [], []

    def api(request):
        if request.url.path == '/v1/files':
            return httpx.Response(201, json={'tenant_id': 'eval', 'id': str(uuid4())})
        assert request.url.path == '/v1/tasks'
        submissions.append(request.headers['Idempotency-Key'])
        if len(submissions) == 1:
            return httpx.Response(503)
        return httpx.Response(201, json={'tenant_id': 'eval', 'id': task_id})

    monkeypatch.setattr(evaluation, 'http', lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(api), base_url='http://test'))

    @activity.defn(name='eval_submit')
    async def submit(request: dict) -> str:
        attempts.append((request['case']['id'], activity.info().attempt))
        return await evaluation.submit(request)

    @activity.defn(name='eval_collect')
    async def collect(request: dict) -> float:
        assert request['task_id'] == task_id
        (suite_path / 'cases/b/acceptance/hidden.py').write_text('assert False\n')
        return 0.0

    workflows = [EvolutionWorkflow, EvalRunWorkflow]
    queue = f'evolve-suite-change-{uuid4()}'
    async with Worker(temporal_env.client, task_queue=queue, workflows=workflows,
                      activities=[*evolution.all(), evaluation.prepare, submit, collect,
                                  evaluation.finish]):
        async with asyncio.timeout(25):
            handle = await temporal_env.client.start_workflow(
                EvolutionWorkflow.run,
                {'id': str(evolution_id), 'tenant_id': 'eval', 'created_by': 'test',
                 'config': config, 'suite_hash': load_suite(suite_path)['hash']},
                id=f'evolve-{evolution_id}', task_queue=queue, execution_timeout=timedelta(seconds=30),
            )
            assert await handle.result() == 'failed'
    assert attempts == [('a', 1), ('a', 2), ('b', 1)]
    assert len(submissions) == 2 and submissions[0] == submissions[1]

    search = await scoped.get_evolution(str(evolution_id))
    assert search['status'] == 'failed'
    [entry] = search['journal']
    run_id = str(entry['id'])
    run = await scoped.get_eval_run(run_id)
    assert run['status'] == 'failed'
    assert run['reason'] == 'Suite changed after evaluation started'
    assert run_id in search['reason'] and run['reason'] in search['reason']
    eval_handle = temporal_env.client.get_workflow_handle(f'eval-{run_id}')
    assert await eval_handle.result() == 'failed'

    replayer = Replayer(workflows=workflows)
    for completed in (handle, eval_handle):
        await replayer.replay_workflow(await completed.fetch_history())


@pytest.mark.parametrize("scenario,expected", [
    ("baseline_errors", "failed"),
    ("baseline_majority_errors", "failed"),
    ("candidate_dev_errors", "completed"),
    ("candidate_holdout_errors", "completed"),
    ("zero_dev", "completed"),
    ("zero_holdout", "completed"),
    ("activity_error", "failed"),
])
async def test_dsh_evolution_integrity_persists_decisions(temporal_env, store, tmp_path, monkeypatch,
                                                         scenario, expected):
    from sca.api.evals import summary

    suite = tmp_path / "suite"
    for case_id, split in (("a", "dev"), ("b", "dev"), ("c", "dev"), ("d", "holdout")):
        case = suite / "cases" / case_id
        (case / "data").mkdir(parents=True)
        (case / "data/input.csv").write_text("value\n1\n", encoding="utf-8")
        (case / "acceptance").mkdir()
        (case / "acceptance/check.py").write_text("assert True\n", encoding="utf-8")
        (case / "acceptance.yaml").write_text("command: python check.py\n", encoding="utf-8")
        (case / "task.yaml").write_text(f"split: {split}\nquestion: Count rows\n", encoding="utf-8")
    (suite / "suite.yaml").write_text("name: test\nkind: analysis\n", encoding="utf-8")
    cfg = EvolutionConfig(suite=str(suite), agent_backend="dsh", generations=1, candidates=1, repeat=1)
    evolution_id = uuid4()
    scoped = store.for_tenant("eval")
    await scoped.create_evolution({"id": evolution_id, "status": "queued", "config": cfg.model_dump(),
                                  "created_by": "test"})
    settings = Settings(_env_file=None, data_dir=tmp_path / "state")
    evaluation, evolution = EvalActivities(settings, store), EvolutionActivities(settings, store, None)
    tasks = {}

    def task_api(request):
        path = request.url.path.split("/")
        task, events = tasks[path[3]]
        return httpx.Response(200, json=events if path[-1] == "events" else task)

    monkeypatch.setattr(evaluation, "http", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(task_api), base_url="http://test",
    ))

    @activity.defn(name="evolve_initialize")
    async def initialize(request: dict) -> dict:
        if scenario == "activity_error":
            raise ApplicationError("fixture database diagnostic", type="UndefinedColumnError", non_retryable=True)
        return await evolution.initialize(request)

    @activity.defn(name="evolve_propose")
    async def propose(request: dict) -> list[dict]:
        return [{"hypothesis": "verify", "overlay": {"tool_guidance": "verify"},
                 "overlay_sha256": "hash", "diagnosis": {}, "static_check": {"valid": True}}]

    @activity.defn(name="eval_submit")
    async def submit(request: dict) -> str:
        split, case_id = request["split"], request["case"]["id"]
        candidate = bool(request["variant"]["agent_overlay"])
        errored = (
            (not candidate and scenario == "baseline_errors")
            or (not candidate and scenario == "baseline_majority_errors" and case_id != "c")
            or (candidate and scenario == f"candidate_{split}_errors")
        )
        cost = 0 if errored or scenario == f"zero_{split}" else (500 if candidate else 1000)
        task, _ = await scoped.create_task(
            tenant_id="eval", kind="analysis", spec={"eval_run_id": request["id"]},
            idempotency_key=f"{request['id']}:{case_id}:{request['iteration']}",
        )
        task_id = str(task.id)
        tasks[task_id] = ({
            **task.model_dump(mode="json"), "state": "FAILED" if errored else "SUCCEEDED",
            "result": {"acceptance_passed": True, "verification_passed": True},
            "usage": {"cost_micro_usd": cost},
        }, [{"seq": 1, "type": "agent.error" if errored else "agent.turn_completed", "payload": {}}])
        return task_id

    queue = f"evolve-integrity-{uuid4()}"
    activities = [initialize, propose, *(item for item in evolution.all()
                                       if item not in (evolution.initialize, evolution.propose)),
                  evaluation.prepare, submit, evaluation.collect, evaluation.finish]
    async with Worker(temporal_env.client, task_queue=queue, workflows=[EvolutionWorkflow, EvalRunWorkflow],
                      activities=activities):
        async with asyncio.timeout(40):
            result = await temporal_env.client.execute_workflow(
                EvolutionWorkflow.run, {"id": str(evolution_id), "tenant_id": "eval", "created_by": "test",
                                        "config": cfg.model_dump(), "suite_hash": load_suite(suite)["hash"]},
                id=f"evolve-{evolution_id}", task_queue=queue,
            )
    assert result == expected
    row = await scoped.get_evolution(str(evolution_id))
    assert row["status"] == expected
    assert row["best_champion_id"] is None
    assert await scoped.get_active_champion(settings.dsh_version, cfg.model, agent_backend="dsh") is None
    if scenario == "activity_error":
        assert "UndefinedColumnError" in row["reason"] and "fixture database diagnostic" not in row["reason"]
        return
    failed_runs = [entry for entry in row["journal"] if entry.get("status") == "failed"]
    if scenario.startswith("baseline_"):
        assert not row["candidates"]
        [entry] = failed_runs
        run_id = str(entry["id"])
        run = await scoped.get_eval_run(run_id)
        counts = summary(run)
        failures = 3 if scenario == "baseline_errors" else 2
        assert (counts["errored"], counts["passed"], counts["failed"]) == (failures, 3 - failures, 0)
        assert sum(case["status"] == "errored" and case["passed"] is None for case in run["results"]) == failures
        assert run_id in row["reason"] and run["reason"] in row["reason"]
    else:
        [candidate] = row["candidates"]
        assert candidate["decision"] == "rejected"
        if scenario.startswith("candidate_"):
            [failed] = failed_runs
            run = await scoped.get_eval_run(str(failed["id"]))
            assert failed["id"] == candidate[f'{failed["split"]}_run_id']
            assert run["reason"] in candidate["reason"]
        else:
            assert not failed_runs
            split = scenario.split("_")[1]
            assert candidate[f"{split}_metrics"]["cost_usd"] == 0
