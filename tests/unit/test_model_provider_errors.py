import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError, RetryState

from sca.agents.opencode import OpenCodeBackend, parse_opencode_line
from sca.agents.pi import parse_pi_line
from sca.api.evals import summary
from sca.config import Settings
from sca.core.model_provider import ModelProviderError, classify_model_error
from sca.core.models import ExecExit, ExecOutput
from sca.eval.activities import EvalActivities
from sca.eval.compare import compare
from sca.eval.workflow import EvalRunWorkflow
from sca.gateway.health import ProviderHealth
from sca.gateway.litellm import GatewayError, LiteLLMGateway
from sca.kernel import activities
from sca.kernel.activities import KernelActivities, TurnInput
from sca.kernel.workflow import TaskWorkflow
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.workflow import _gather_subtasks
from sca.orchestration.provider_errors import account_failure


@pytest.mark.parametrize("status,body,kind", [
    (402, "", "billing"),
    (402, '{"error":{"message":"DeepseekException: Insufficient Balance"}}', "billing"),
    (400, '{"detail":"Your quota has been exhausted"}', "billing"),
    (429, '{"error":{"code":"insufficient_quota"}}', "billing"),
    (401, '{"error":{"message":"Invalid API key"}}', "auth"),
    (403, "Forbidden", "auth"),
    (429, '{"error":{"code":"rate_limit_exceeded"}}', "rate_limit"),
    (503, "Service Unavailable", "unavailable"),
    (400, '{"error":{"type":"BudgetExceededError","message":"Budget has been exceeded"}}', None),
    (400, '{"error":{"message":"Unknown model"}}', None),
])
async def test_classifies_litellm_response_without_exposing_body(status, body, kind):
    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(status, text=body + " PRIVATE_FIXTURE_TEXT")

    gateway = LiteLLMGateway("http://gateway", "test-key", "http://sandbox")
    await gateway.aclose()
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(respond)) as http:
        gateway._http = http
        with pytest.raises(GatewayError) as failure:
            await gateway.complete([], "fixture-model")
    error = failure.value
    if kind is None:
        assert type(error) is GatewayError
    else:
        assert isinstance(error, ModelProviderError)
        assert error.kind == kind and error.status == status
        assert len(error.provider_message_redacted) < 80
        assert error.is_account_error == (kind in {"billing", "auth"})
    assert "PRIVATE_FIXTURE_TEXT" not in str(error)


async def test_gateway_health_tracks_errors_and_only_clears_after_a_valid_completion(tmp_path):
    outcomes = iter([402, 429, None, 200])

    def respond(request):
        if request.url.path == "/key/delete":
            return httpx.Response(200, json={})
        status = next(outcomes)
        if status is None:
            raise httpx.ReadTimeout("PRIVATE_FIXTURE_TEXT", request=request)
        return httpx.Response(status, json={"choices": [{"message": {"content": "ok"}}]})

    health = ProviderHealth(tmp_path)
    gateway = LiteLLMGateway("http://gateway", "test-key", "http://sandbox", provider_health=health)
    await gateway.aclose()
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(respond)) as http:
        gateway._http = http
        with pytest.raises(ModelProviderError) as failure:
            await gateway.complete([], "fixture-model")
        assert failure.value.kind == "billing"
        observed = ProviderHealth(tmp_path)
        original = observed.read()["account_error"]
        assert set(original) == {"kind", "at"}
        assert datetime.fromisoformat(original["at"]).tzinfo == UTC
        await gateway._post("/key/delete", {"key_aliases": ["test-alias"]})
        assert observed.read()["account_error"] == original
        for kind, status in [("rate_limit", 429), ("unavailable", None)]:
            with pytest.raises(ModelProviderError) as failure:
                await gateway.complete([], "fixture-model")
            assert (failure.value.kind, failure.value.status) == (kind, status)
            assert "PRIVATE_FIXTURE_TEXT" not in str(failure.value)
            assert observed.read()["last_error"]["kind"] == kind
            assert observed.read()["account_error"] == original
        assert await gateway.complete([], "fixture-model") == "ok"
    assert observed.read() == {}


def test_agent_error_parser_uses_structured_model_errors_only():
    error = {"type": "provider.rate-limit", "status": 429, "message": "Too many requests",
             "response": {"body": '{"error":{"code":"insufficient_quota","secret":"PRIVATE"}}'}}
    (event,) = parse_opencode_line(json.dumps({"type": "error", "error": error}))
    assert event.data["model_provider_error"]["kind"] == "billing"
    assert event.raw is None and "PRIVATE" not in str(event)
    (tool_error,) = parse_opencode_line(json.dumps({"type": "error", "error": {
        "type": "tool.execution", "message": "tool printed HTTP 402 Insufficient Balance"}}))
    assert "model_provider_error" not in tool_error.data
    (pi_error,) = parse_pi_line(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "error", "errorMessage": "HTTP 401: Invalid API key PRIVATE"}}))
    assert pi_error.data["model_provider_error"]["kind"] == "auth"
    assert "PRIVATE" not in str(pi_error)
    assert classify_model_error({"type": "provider.timeout"}).kind == "unavailable"


def failed_activity(name, failure):
    error = ActivityError("Activity failed", scheduled_event_id=1, started_event_id=2,
                          identity="test", activity_type=name, activity_id="1",
                          retry_state=RetryState.NON_RETRYABLE_FAILURE)
    error.__cause__ = failure
    return error


@pytest.mark.parametrize("kind,status,label", [
    ("billing", 402, "模型服务账户余额不足（402）"),
    ("auth", 401, "模型密钥无效（401）"),
])
async def test_account_error_fails_task_before_fix_attempts(kind, status, label, tmp_path, monkeypatch):
    class Sandbox:
        recovered = False

        async def exec(self, *args, **kwargs):
            if self.recovered:
                yield ExecOutput(stream="stdout", text=json.dumps({"type": "step_finish", "part": {
                    "reason": "stop", "tokens": {"input": 1, "output": 1}}}))
                yield ExecExit(exit_code=0)
                return
            yield ExecOutput(stream="stdout", text=json.dumps({"type": "error", "error": {
                "type": "provider.quota" if kind == "billing" else "provider.auth", "status": status,
                "response": {"body": "PRIVATE_FIXTURE_TEXT"},
            }}))
            yield ExecExit(exit_code=1)

    version_dir = tmp_path / "2.3.0-sca.1"
    version_dir.mkdir()
    (version_dir / "opencode").touch()
    gateway = SimpleNamespace(sandbox_url="http://gateway", issue_key=AsyncMock(return_value="ephemeral"),
                              finalize_key=AsyncMock(return_value=(0, True)))
    store = SimpleNamespace(append_event=AsyncMock(), add_usage=AsyncMock(),
                            get_session_id=AsyncMock(return_value=None),
                            get_task=AsyncMock(return_value=SimpleNamespace(kind="coding")))
    sandbox = Sandbox()
    kernel = KernelActivities(settings=Settings(_env_file=None, data_dir=tmp_path), store=store,
                              sandbox=sandbox, agent=OpenCodeBackend(tmp_path, version_dir.name, "model"),
                              objects=SimpleNamespace(), gateway=gateway)
    kernel._ensure_state = AsyncMock(return_value=False)
    kernel._resolve_opencode_version = AsyncMock()
    kernel._sandbox_id = AsyncMock(return_value="sandbox")
    kernel._spec = AsyncMock(return_value=CodingSpec(repo_url="repo", instruction="fix"))
    kernel.transport.pull_agent_home = AsyncMock()
    monkeypatch.setattr(activities.activity, "info", lambda: SimpleNamespace(attempt=1))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda: None)
    calls, states, patches = [], [], []

    async def execute(name, arg=None, **options):
        calls.append(name)
        if name == "load_task":
            return {"max_fix_attempts": 3, "turn_timeout_s": 30,
                    "hooks": {name: name for name in ("prepare", "initial_prompt", "checkpoint", "verify")}}
        if name == "initial_prompt":
            return "instructions"
        if name == "set_state":
            states.append(arg)
        if name == "check_budget" and arg.turn:
            return "Task cost budget exhausted"
        if name == "run_agent_turn":
            try:
                return await kernel._run_agent_turn(arg)
            except ApplicationError as exc:
                assert exc.non_retryable
                assert "PRIVATE_FIXTURE_TEXT" not in str(exc)
                raise failed_activity(name, exc) from exc

    def patched(patch):
        patches.append(patch)
        return True

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "patched", patched)
    with pytest.raises(ApplicationError, match=label) as failure:
        await TaskWorkflow().run("task")
    assert failure.value.type == "TaskFailed"
    assert calls.count("run_agent_turn") == 1 and calls.count("check_budget") == 1
    assert not {"checkpoint", "verify", "fix_prompt"}.intersection(calls)
    assert "model-provider-errors" in patches
    saved = states[-1]
    assert saved.state == "FAILED" and label in saved.reason
    assert saved.model_provider_error == ModelProviderError(kind, status).payload()
    (event,) = [call for call in store.append_event.await_args_list if call.args[1] == "model.provider_error"]
    assert event.args[2] == {"turn": 1, "attempt": 1, **saved.model_provider_error}
    assert event.kwargs["dedup_key"] == "turn1.attempt1.provider_error"
    gateway.finalize_key.assert_awaited_once()
    assert ProviderHealth(tmp_path).read()["account_error"]["kind"] == kind
    sandbox.recovered = True
    await kernel._run_agent_turn(TurnInput("retry-task", 1, "instructions"))
    assert ProviderHealth(tmp_path).read() == {}


async def test_account_failure_lets_parallel_subtasks_clean_up(monkeypatch):
    started, cleaned = asyncio.Event(), asyncio.Event()
    activity = failed_activity("coding.subtask_step", account_failure(ModelProviderError("auth", 401)))
    error = ChildWorkflowError("Child workflow failed", namespace="test", workflow_id="subtask",
                               run_id="run", workflow_type="CodingSubtaskWorkflow", initiated_event_id=1,
                               started_event_id=2, retry_state=None)

    async def failed():
        await started.wait()
        raise error from activity

    async def sibling():
        try:
            started.set()
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(workflow, "patched", lambda patch: patch == "model-provider-errors")
    with pytest.raises(ChildWorkflowError):
        await _gather_subtasks(failed(), sibling())
    assert cleaned.is_set()


async def test_eval_records_infrastructure_error_and_stops_submitting(monkeypatch):
    provider = ModelProviderError("billing", 402)
    case_task = {name: str(uuid4()) for name in ("ordinary", "billing", "pending")}
    run_id, submitted = str(uuid4()), []
    store = SimpleNamespace(record_eval_result=AsyncMock(), finish_eval_run=AsyncMock())
    activities = EvalActivities(Settings(_env_file=None), store)

    def respond(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"seq": 1, "type": "agent.turn_started", "payload": {"turn": 1}}])
        task_id = request.url.path.rsplit("/", 1)[-1]
        failed = task_id == case_task["billing"]
        return httpx.Response(200, json={"id": task_id, "state": "FAILED" if failed else "SUCCEEDED",
            "created_at": "2026-11-21T01:00:00Z", "updated_at": "2026-11-21T01:00:04Z",
            "usage": {"cost_micro_usd": 1000}, "result": {"model_provider_error": provider.payload()}
            if failed else {"acceptance_passed": True, "verification_passed": True}})

    activities.http = lambda: httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(respond))

    async def execute(name, item, **options):
        if name == "eval_prepare":
            return {"cases": [{"id": case} for case in case_task]}
        if name == "eval_submit":
            case_id = item["case"]["id"]
            submitted.append(case_id)
            return case_task[case_id]
        if name == "eval_collect":
            try:
                return await activities.collect(item)
            except ApplicationError as exc:
                raise failed_activity(name, exc) from exc
        assert name == "eval_finish"
        await activities.finish(item)

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "patched", lambda patch: patch == "model-provider-errors")
    request = {"id": run_id, "repeat": 1, "concurrency": 2, "budget": 1, "case_budget": .1}
    assert await EvalRunWorkflow().run(request) == "failed"
    assert submitted == ["ordinary", "billing"]
    store.finish_eval_run.assert_awaited_once_with(run_id, "failed", reason=provider.reason)
    normal, rejected = (call.args[0] for call in store.record_eval_result.await_args_list)
    assert normal["acceptance_passed"] is True
    assert rejected["acceptance_passed"] is rejected["trajectory_passed"] is rejected["verification_passed"] is None
    assert rejected["turns"] == 1 and rejected["cost_usd"] == .001

    comparable = {"suite_hash": "same", "results": [normal, {**rejected, "acceptance_passed": False}]}
    interrupted = {"suite_hash": "same", "results": [normal, {**rejected, "infrastructure_error": provider.payload()}]}
    counts = summary(interrupted)
    assert (counts["passed"], counts["failed"], counts["infrastructure_errors"]) == (1, 0, 1)
    report = compare(comparable, interrupted)
    assert report["B"]["pass_rate"]["mean"] == 1
    assert report["A_wins"] == report["B_wins"] == 0
    assert report["cases"][0] == {"case": "billing", "A": None, "B": None}
