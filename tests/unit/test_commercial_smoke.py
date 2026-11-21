from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest

from scripts import commercial_smoke

OPERATOR = "fixture-operator-credential"
ADMIN = "fixture-admin-credential"
MEMBER = "fixture-member-credential"
VIEWER = "fixture-viewer-credential"
PRIVATE_TEXT = f"{OPERATOR} {ADMIN} {MEMBER} {VIEWER}"
RUN_ID = UUID("d0a6205f-5d27-4440-844e-757b09519078")
TENANT = f"commercial-smoke-{RUN_ID.hex}"
TENANT_PATH = f"/v1/admin/tenants/{TENANT}"
TASK_ID = "3e30a217-af72-4216-a736-16ef2ada11e4"
TASK_PATH = f"/v1/tasks/{TASK_ID}"
STEPS = (
    "tenant-create", "tenant-bootstrap", "tenant-quota", "members", "token-roles", "coding",
    "coding-review", "usage", "usage-csv", "knowledge", "stripe-checkout", "suspend",
    "suspended-submit", "resume", "cleanup",
)
STRIPE_STEPS = ("stripe-complete", "stripe-payment-failed", "stripe-suspended-submit", "stripe-payment-succeeded")
SIM_URL = "http://stripe.test:12111"
CHECKOUT_PATH = "/_sim/checkout/cs_smoke"
SUBSCRIPTION_PATH = "/_sim/subscriptions/sub_smoke"


@dataclass
class Exchange:
    method: str
    path: str
    token: str | None
    reply: httpx.Response | httpx.RequestError
    body: dict | None = None


def exchange(
    method: str, path: str, token: str | None, data: object = None, *, status: int = 200,
    body: dict | None = None, content: bytes | None = None,
) -> Exchange:
    return Exchange(method, path, token, httpx.Response(status, json=data, content=content), body)


def onboarding() -> list[Exchange]:
    tenant = {"id": TENANT, "plan": "team", "status": "active"}
    member = {"id": "member-id", "tenant_id": TENANT, "role": "member"}
    viewer = {"id": "viewer-id", "tenant_id": TENANT, "role": "viewer"}
    email = "reader@example.invalid"
    return [
        exchange("POST", "/v1/admin/tenants", OPERATOR, tenant, status=201,
                 body={"id": TENANT, "display_name": TENANT, "plan": "team"}),
        exchange("POST", f"{TENANT_PATH}/tokens", OPERATOR,
                 {"token": ADMIN, "role": "admin", "tenant_id": TENANT}, status=201),
        exchange("GET", "/v1/tenant", ADMIN, tenant),
        exchange("GET", "/v1/portal/tenant-quota", ADMIN, {
            "slots_limit": 4, "slots_used": 0, "queued": 0, "monthly_cost_limit_usd": 100,
            "max_task_cost_usd": 10,
        }),
        exchange("POST", "/v1/tenant/members", ADMIN, {"email": email, "role": "viewer"},
                 status=201, body={"email": email, "role": "viewer"}),
        exchange("PATCH", f"/v1/tenant/members/{email}", ADMIN, {"role": "member"},
                 body={"role": "member"}),
        exchange("DELETE", f"/v1/tenant/members/{email}", ADMIN, status=204),
        exchange("GET", "/v1/tenant/members", ADMIN, []),
        exchange("POST", "/v1/tenant/tokens", ADMIN, {**member, "token": MEMBER}, status=201,
                 body={"name": "Commercial smoke member", "role": "member", "days": 1}),
        exchange("POST", "/v1/tenant/tokens", ADMIN, {**viewer, "token": VIEWER}, status=201,
                 body={"name": "Commercial smoke viewer", "role": "viewer", "days": 1}),
        exchange("GET", "/v1/tenant/tokens", ADMIN, [member, viewer]),
        exchange("POST", "/v1/tasks", VIEWER, {"error": {"code": "forbidden", "message": PRIVATE_TEXT}},
                 status=403),
    ]


def coding(*, verified: bool = True, already_succeeded: bool = False) -> list[Exchange]:
    queued = {"id": TASK_ID, "tenant_id": TENANT, "state": "QUEUED"}
    published = {**queued, "state": "SUCCEEDED" if already_succeeded else "AWAITING_REVIEW",
                 "result": {"verification_passed": verified, "patch_artifact": "changes.patch"}}
    calls = [
        exchange("POST", "/v1/tasks", MEMBER, queued, status=201),
        exchange("GET", TASK_PATH, MEMBER, queued),
        exchange("GET", TASK_PATH, MEMBER, published),
    ]
    if verified:
        calls += [
            exchange("GET", f"{TASK_PATH}/artifacts/changes.patch", MEMBER, content=b"patch"),
            exchange("GET", f"{TASK_PATH}/impact", MEMBER, {"changed_paths": [f"{TENANT}.txt"]}),
        ]
    if verified and not already_succeeded:
        calls += [
            exchange("POST", f"{TASK_PATH}/review", MEMBER, published, body={"decision": "approve"}),
            exchange("GET", TASK_PATH, MEMBER, {**published, "state": "SUCCEEDED"}),
        ]
    return calls


def accounting(task_count: int, *, stripe_enabled: bool, stripe_sim: bool = False) -> list[Exchange]:
    calls = [exchange("GET", "/v1/usage", MEMBER, [])]
    if task_count:
        calls.append(exchange("GET", "/v1/usage", MEMBER, [{"tenant_id": TENANT, "tasks": 1}]))
    calls += [
        exchange("GET", "/v1/usage/export.csv", MEMBER,
                 content=f"tenant_id,plan,tasks\n{TENANT},team,{task_count}\n".encode()),
        exchange("POST", "/v1/knowledge/documents", ADMIN, {"id": "knowledge-id"}, status=201,
                 body={"title": "Commercial readiness", "text": TENANT, "format": "text"}),
        exchange("POST", "/v1/knowledge/search", MEMBER, [
            {"document_id": "knowledge-id", "text": TENANT, "citation": "knowledge-id#chunk-0"},
        ], body={"query": TENANT}),
        exchange("GET", "/v1/tenant/billing", ADMIN, {"enabled": stripe_enabled}),
    ]
    if stripe_enabled:
        checkout_url = SIM_URL + CHECKOUT_PATH if stripe_sim else f"https://checkout.example.test/{VIEWER}"
        calls.append(exchange("POST", "/v1/tenant/billing/checkout", ADMIN,
                              {"url": checkout_url}, body={"plan": "team"}))
    return calls


def stripe_payments() -> list[Exchange]:
    tenant = {"id": TENANT, "plan": "team", "status": "active"}
    billing = {"subscription_status": "active", "suspended_for_nonpayment": False}
    invoice = {"id": "in_smoke", "subscription_id": "sub_smoke", "amount_due": 2900}
    paid = {**invoice, "status": "paid", "amount_paid": 2900}
    calls = [
        exchange("PATCH", TENANT_PATH, OPERATOR, {**tenant, "plan": "free"}, body={"plan": "free"}),
        exchange("POST", f"{CHECKOUT_PATH}/complete", None, {"subscription": "sub_smoke"}),
        exchange("GET", "/v1/tenant", ADMIN, tenant),
        exchange("GET", "/v1/tenant/billing", ADMIN, billing),
    ]
    for attempt in (1, 2, 3):
        suspended = attempt == 3
        calls += [
            exchange("POST", f"{SUBSCRIPTION_PATH}/payment_failed", None,
                     {**invoice, "status": "open", "attempt_count": attempt}),
            exchange("GET", "/v1/tenant", ADMIN, {**tenant, "status": "suspended" if suspended else "active"}),
            exchange("GET", "/v1/tenant/billing", ADMIN, {"suspended_for_nonpayment": suspended}),
        ]
    return calls + [
        exchange("POST", "/v1/tasks", MEMBER, {"error": {"code": "tenant_suspended"}}, status=403),
        exchange("POST", f"{SUBSCRIPTION_PATH}/payment_succeeded", None, paid),
        exchange("GET", "/v1/tenant", ADMIN, tenant),
        exchange("GET", "/v1/tenant/billing", ADMIN, {**billing, "invoices": [paid]}),
    ]


def suspension(*, delete_status: int) -> list[Exchange]:
    suspended = {"id": TENANT, "status": "suspended"}
    calls = [
        exchange("POST", f"{TENANT_PATH}/suspend", OPERATOR, suspended),
        exchange("POST", "/v1/tasks", MEMBER, {"error": {"code": "tenant_suspended"}}, status=403),
        exchange("POST", f"{TENANT_PATH}/resume", OPERATOR, {**suspended, "status": "active"}),
        exchange("DELETE", TENANT_PATH, OPERATOR, status=delete_status),
    ]
    if delete_status != 204:
        calls.append(exchange("POST", f"{TENANT_PATH}/suspend", OPERATOR, suspended))
    return calls


def invoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], calls: list[Exchange],
    *, with_agent: bool, stripe_sim: bool = False,
) -> tuple[int, dict[str, str], str]:
    pending = deque(calls)

    def handle(request: httpx.Request) -> httpx.Response:
        step = pending.popleft()
        assert (request.method, request.url.path) == (step.method, step.path)
        if step.token is None:
            assert (request.url.host, request.url.port) == ("stripe.test", 12111)
            assert "Authorization" not in request.headers
        else:
            assert request.url.host == "sca.test"
            assert request.headers["Authorization"] == f"Bearer {step.token}"
        if step.body is not None:
            assert json.loads(request.content) == step.body
        if request.method == "POST" and request.url.path == "/v1/tasks":
            spec = json.loads(request.content)["spec"]
            if isinstance(step.reply, httpx.Response) and step.reply.status_code == 201:
                assert spec["repo_url"] == "/srv/smoke.git"
                assert spec["base_ref"] == "HEAD"
                assert spec["best_of"] is None
                assert spec["push"] is spec["auto_finish"] is False
                assert f"{TENANT}.txt" in spec["verify_command"]
            else:
                assert spec["repo_url"].startswith("/nonexistent/")
        if isinstance(step.reply, httpx.RequestError):
            raise step.reply
        return step.reply

    async_client = httpx.AsyncClient
    monkeypatch.setattr(commercial_smoke.httpx, "AsyncClient",
                        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(commercial_smoke, "uuid4", lambda: RUN_ID)
    monkeypatch.setattr(commercial_smoke, "POLL_SECONDS", 0)
    monkeypatch.setenv("SCA_PLATFORM_ADMIN_TOKEN", OPERATOR)
    argv = ["--api", "http://sca.test"]
    if with_agent:
        argv += ["--with-agent", "--repo", "/srv/smoke.git"]
    if stripe_sim:
        argv += ["--stripe-sim", SIM_URL]
    status = commercial_smoke.main(argv)
    assert not pending
    output = capsys.readouterr()
    assert all(token not in output.out + output.err for token in (OPERATOR, ADMIN, MEMBER, VIEWER))
    lines = [line.split(maxsplit=2) for line in output.out.splitlines()]
    after_checkout = STEPS.index("stripe-checkout") + 1
    expected = STEPS[:after_checkout] + STRIPE_STEPS + STEPS[after_checkout:] if stripe_sim else STEPS
    assert tuple(line[1] for line in lines) == expected
    results = {step: result for result, step, _ in lines}
    return status, results, output.out


@pytest.mark.parametrize(("with_agent", "already_succeeded"), [(True, False), (True, True), (False, False)])
def test_commercial_smoke_sequence(monkeypatch, capsys, with_agent, already_succeeded):
    calls = onboarding()
    if with_agent:
        calls += coding(already_succeeded=already_succeeded)
    calls += accounting(int(with_agent), stripe_enabled=with_agent)
    calls += suspension(delete_status=204 if with_agent else 405)
    status, results, output = invoke(monkeypatch, capsys, calls, with_agent=with_agent)
    skips = set() if with_agent else {"coding", "coding-review", "stripe-checkout"}
    if already_succeeded:
        skips.add("coding-review")
    assert status == 0
    assert results == {step: "SKIP" if step in skips else "PASS" for step in STEPS}
    if not with_agent:
        assert "SKIP stripe-checkout Stripe is disabled" in output
        assert "PASS cleanup tenant suspended" in output


def test_transport_error_hides_credentials_and_cleans_up(monkeypatch, capsys):
    calls = onboarding()
    calls[-1].reply = httpx.ConnectError(PRIVATE_TEXT)
    calls.append(exchange("DELETE", TENANT_PATH, OPERATOR, status=204))
    status, results, output = invoke(monkeypatch, capsys, calls, with_agent=False)
    assert status == 1
    assert results["token-roles"] == "FAIL"
    assert results["knowledge"] == "SKIP"
    assert results["cleanup"] == "PASS"
    assert "FAIL token-roles API request failed" in output


def test_unverified_task_is_cancelled_before_tenant_cleanup(monkeypatch, capsys):
    calls = onboarding() + coding(verified=False)
    calls += [
        exchange("POST", f"{TASK_PATH}/cancel", ADMIN, {"id": TASK_ID}),
        exchange("DELETE", TENANT_PATH, OPERATOR, status=204),
    ]
    status, results, output = invoke(monkeypatch, capsys, calls, with_agent=True)
    assert status == 1
    assert results["coding"] == "FAIL"
    assert results["coding-review"] == "SKIP"
    assert results["cleanup"] == "PASS"
    assert "FAIL coding coding task did not pass verification" in output


def test_suspended_submission_requires_specific_error_code(monkeypatch, capsys):
    suspend, submit, _, delete = suspension(delete_status=204)
    submit.reply = httpx.Response(403, json={"error": {"code": "forbidden", "message": PRIVATE_TEXT}})
    calls = onboarding() + accounting(0, stripe_enabled=False) + [suspend, submit, delete]
    status, results, output = invoke(monkeypatch, capsys, calls, with_agent=False)
    assert status == 1
    assert results["suspended-submit"] == "FAIL"
    assert results["resume"] == "SKIP"
    assert results["cleanup"] == "PASS"
    assert "FAIL suspended-submit expected 403 tenant_suspended" in output


def test_stripe_smoke_completes_billing_workflow_and_cancels_subscription(monkeypatch, capsys):
    calls = onboarding() + accounting(0, stripe_enabled=True, stripe_sim=True) + stripe_payments()
    *manual, delete = suspension(delete_status=204)
    calls += [*manual, exchange("POST", f"{SUBSCRIPTION_PATH}/cancel", None, {}), delete]
    status, results, _ = invoke(monkeypatch, capsys, calls, with_agent=False, stripe_sim=True)
    assert status == 0
    assert all(results[step] == "PASS" for step in ("stripe-checkout", *STRIPE_STEPS, "cleanup"))


def test_stripe_smoke_skips_when_billing_is_disabled(monkeypatch, capsys):
    calls = onboarding() + accounting(0, stripe_enabled=False) + suspension(delete_status=204)
    status, results, _ = invoke(monkeypatch, capsys, calls, with_agent=False, stripe_sim=True)
    assert status == 0
    assert all(results[step] == "SKIP" for step in ("stripe-checkout", *STRIPE_STEPS))


def test_stripe_smoke_does_not_follow_checkout_on_a_different_origin(monkeypatch, capsys):
    calls = onboarding() + accounting(0, stripe_enabled=True)
    calls.append(exchange("DELETE", TENANT_PATH, OPERATOR, status=204))
    status, results, output = invoke(monkeypatch, capsys, calls, with_agent=False, stripe_sim=True)
    assert status == 1
    assert results["stripe-checkout"] == "FAIL"
    assert all(results[step] == "SKIP" for step in STRIPE_STEPS)
    assert results["cleanup"] == "PASS"
    assert "checkout URL does not match --stripe-sim" in output
