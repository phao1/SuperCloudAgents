"""Check basic commercial readiness against a running SCA API."""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from io import StringIO
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

POLL_SECONDS = 2
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class CheckFailed(Exception):
    """A failure with a diagnostic safe to print."""


class CheckSkipped(Exception):
    """A check whose optional prerequisite is missing."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


async def report(step: str, check: Callable[[], Awaitable[str]]) -> bool:
    try:
        detail = await check()
    except CheckSkipped as exc:
        print(f"SKIP {step} {exc}", flush=True)
        return True
    except CheckFailed as exc:
        detail = str(exc)
    except TimeoutError:
        detail = "polling deadline exceeded"
    except httpx.RequestError:
        detail = "API request failed"
    except Exception:  # noqa: BLE001
        # Response bodies and exception text can contain credentials.
        detail = "unexpected API response or client error"
    else:
        print(f"PASS {step} {detail}", flush=True)
        return True
    print(f"FAIL {step} {detail}", flush=True)
    return False


class CommercialSmoke:
    def __init__(
        self, http: httpx.AsyncClient, operator_token: str, repo: str | None, wait_seconds: int,
        stripe_sim: httpx.URL | None = None,
    ) -> None:
        self.http = http
        self.operator_token = operator_token
        self.repo = repo
        self.wait_seconds = wait_seconds
        self.tenant_id = f"commercial-smoke-{uuid4().hex}"
        self.tenant_path = f"/v1/admin/tenants/{self.tenant_id}"
        self.tenant_created = False
        self.admin_token = ""
        self.member_token = ""
        self.task_path: str | None = None
        self.task_terminal = False
        self.started_on = datetime.now(UTC).date()
        self.stripe_sim = stripe_sim
        self.stripe_checkout: str | None = None
        self.stripe_subscription: str | None = None
        self.stripe_invoice: str | None = None

    async def run(self) -> int:
        stripe_steps = (
            ("stripe-complete", self.complete_stripe_checkout),
            ("stripe-payment-failed", self.check_stripe_payment_failures),
            ("stripe-suspended-submit", self.check_stripe_suspension),
            ("stripe-payment-succeeded", self.check_stripe_payment),
        ) if self.stripe_sim is not None else ()
        steps = (
            ("tenant-create", self.create_tenant),
            ("tenant-bootstrap", self.bootstrap_tenant),
            ("tenant-quota", self.check_quota),
            ("members", self.check_members),
            ("token-roles", self.check_tokens),
            ("coding", self.check_coding),
            ("coding-review", self.approve_task),
            ("usage", self.check_usage),
            ("usage-csv", self.check_csv),
            ("knowledge", self.check_knowledge),
            ("stripe-checkout", self.check_stripe),
            *stripe_steps,
            ("suspend", self.suspend_tenant),
            ("suspended-submit", self.check_suspension),
            ("resume", self.resume_tenant),
        )
        passed = True
        try:
            for step, check in steps:
                if passed:
                    passed = await report(step, check)
                else:
                    print(f"SKIP {step} previous step failed", flush=True)
        finally:
            if self.tenant_created:
                cleaned_up = await report("cleanup", self.cleanup)
                passed = passed and cleaned_up
            else:
                print(f"SKIP cleanup creation of {self.tenant_id} unconfirmed", flush=True)
        return 0 if passed else 1

    async def request(
        self, method: str, path: str, token: str, *, expected: tuple[int, ...] = (200,),
        body: dict[str, object] | None = None, params: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = await self.http.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body, params=params,
        )
        if response.status_code not in expected:
            codes = "/".join(map(str, expected))
            raise CheckFailed(f"{method} returned HTTP {response.status_code}, expected {codes}")
        return response

    async def create_tenant(self) -> str:
        response = await self.request(
            "POST", "/v1/admin/tenants", self.operator_token, expected=(201,),
            body={"id": self.tenant_id, "display_name": self.tenant_id, "plan": "team"},
        )
        self.tenant_created = True
        tenant = response.json()
        require(tenant["id"] == self.tenant_id and tenant["plan"] == "team", "wrong tenant or plan")
        return self.tenant_id

    async def issue_token(self, path: str, issuer: str, role: str) -> dict[str, Any]:
        response = await self.request(
            "POST", path, issuer, expected=(201,),
            body={"name": f"Commercial smoke {role}", "role": role, "days": 1},
        )
        issued = response.json()
        require(issued["role"] == role and issued["tenant_id"] == self.tenant_id, "wrong token scope")
        require(isinstance(issued["token"], str) and issued["token"], "missing issued token")
        return issued

    async def bootstrap_tenant(self) -> str:
        issued = await self.issue_token(f"{self.tenant_path}/tokens", self.operator_token, "admin")
        self.admin_token = issued["token"]
        return "issued tenant administrator credential"

    async def check_quota(self) -> str:
        tenant = (await self.request("GET", "/v1/tenant", self.admin_token)).json()
        require(tenant["id"] == self.tenant_id and tenant["plan"] == "team", "wrong tenant or plan")
        quota = (await self.request("GET", "/v1/portal/tenant-quota", self.admin_token)).json()
        require(quota["slots_limit"] > 0 and quota["slots_used"] == quota["queued"] == 0,
                "unexpected quota for a new tenant")
        require("monthly_cost_limit_usd" in quota and "max_task_cost_usd" in quota,
                "missing plan cost limits")
        return "read tenant identity and team quota"

    async def check_members(self) -> str:
        path = "/v1/tenant/members"
        email = "reader@example.invalid"
        invited = await self.request("POST", path, self.admin_token, expected=(201,),
                                     body={"email": email, "role": "viewer"})
        invitation = invited.json()
        require(invitation["email"] == email and invitation["role"] == "viewer",
                "viewer invitation missing")
        member_path = f"{path}/{email}"
        changed = await self.request("PATCH", member_path, self.admin_token, body={"role": "member"})
        require(changed.json()["role"] == "member", "member role did not change")
        await self.request("DELETE", member_path, self.admin_token, expected=(204,))
        members = (await self.request("GET", path, self.admin_token)).json()
        require(isinstance(members, list) and all(row["email"] != email for row in members),
                "member removal was not confirmed by the member list")
        return "invited, changed role and removed member"

    async def check_tokens(self) -> str:
        path = "/v1/tenant/tokens"
        member = await self.issue_token(path, self.admin_token, "member")
        self.member_token = member["token"]
        viewer = await self.issue_token(path, self.admin_token, "viewer")
        listed = (await self.request("GET", path, self.admin_token)).json()
        require(all(row["tenant_id"] == self.tenant_id and "token" not in row and "token_hash" not in row
                    for row in listed), "token inventory exposes credentials or another tenant")
        roles = {row["id"]: row["role"] for row in listed}
        require(roles.get(member["id"]) == "member" and roles.get(viewer["id"]) == "viewer",
                "issued roles missing from token inventory")
        await self.expect_denied(viewer["token"], "forbidden")
        return "issued member and viewer credentials and verified viewer submission denial"

    async def expect_denied(self, token: str, code: str) -> None:
        # A valid spec with an unreachable repo avoids a model call if admission regresses.
        response = await self.request(
            "POST", "/v1/tasks", token, expected=(403, 201),
            body={"kind": "coding", "spec": {
                "repo_url": f"/nonexistent/{self.tenant_id}.git", "instruction": "Readiness probe",
            }},
        )
        if response.status_code == 201:
            self.record_task(response)
            raise CheckFailed("submission was unexpectedly accepted")
        require(response.json()["error"]["code"] == code, f"expected 403 {code}")

    def record_task(self, response: httpx.Response) -> str:
        task = response.json()
        require(task["tenant_id"] == self.tenant_id, "task belongs to another tenant")
        self.task_path = f"/v1/tasks/{UUID(task['id'])}"
        self.task_terminal = task["state"] in TERMINAL_STATES
        return self.task_path

    async def wait_for_task(self, path: str, *states: str) -> dict[str, Any]:
        async with asyncio.timeout(self.wait_seconds):
            while True:
                task = (await self.request("GET", path, self.member_token)).json()
                state = task["state"]
                self.task_terminal = state in TERMINAL_STATES
                if state in states:
                    return task
                require(not self.task_terminal, "task ended before reaching the expected state")
                await asyncio.sleep(POLL_SECONDS)

    async def check_coding(self) -> str:
        if self.repo is None:
            raise CheckSkipped("requires --with-agent and --repo")
        filename = f"{self.tenant_id}.txt"
        response = await self.request(
            "POST", "/v1/tasks", self.member_token, expected=(201,),
            body={"kind": "coding", "spec": {
                "repo_url": self.repo, "base_ref": "HEAD", "best_of": None,
                "push": False, "auto_finish": False, "memory_enabled": False,
                "instruction": (f"Create {filename} in the repository root with one line containing "
                                f"{self.tenant_id}. Leave all other files unchanged."),
                "verify_command": f'test "$(cat {filename})" = "{self.tenant_id}"',
            }},
        )
        path = self.record_task(response)
        task = await self.wait_for_task(path, "AWAITING_REVIEW", "SUCCEEDED")
        result = task["result"]
        require(result["verification_passed"] is True, "coding task did not pass verification")
        name = quote(result["patch_artifact"], safe="")
        patch = await self.request("GET", f"{path}/artifacts/{name}", self.member_token)
        require(patch.content, "patch artifact is empty")
        impact = (await self.request("GET", f"{path}/impact", self.member_token)).json()
        require(impact["changed_paths"] == [filename], "impact did not identify the expected change")
        return "downloaded verified patch and checked changed paths"

    async def approve_task(self) -> str:
        if self.task_path is None:
            raise CheckSkipped("requires --with-agent and --repo")
        if self.task_terminal:
            raise CheckSkipped("task already succeeded before approval")
        await self.request("POST", f"{self.task_path}/review", self.member_token,
                           body={"decision": "approve"})
        await self.wait_for_task(self.task_path, "SUCCEEDED")
        return "approved patch and confirmed task success"

    def usage_dates(self) -> dict[str, str]:
        return {"from": self.started_on.isoformat(), "to": datetime.now(UTC).date().isoformat()}

    async def check_usage(self) -> str:
        expected = int(self.task_path is not None)
        async with asyncio.timeout(self.wait_seconds):
            while True:
                response = await self.request("GET", "/v1/usage", self.member_token,
                                              params=self.usage_dates())
                rows = response.json()
                require(isinstance(rows, list) and all(row["tenant_id"] == self.tenant_id
                                                       for row in rows), "usage must be tenant-scoped")
                count = sum(row["tasks"] for row in rows)
                require(0 <= count <= expected, "unexpected number of metered tasks")
                if count == expected:
                    return f"tenant usage reports {expected} completed task(s)"
                await asyncio.sleep(POLL_SECONDS)

    async def check_csv(self) -> str:
        response = await self.request("GET", "/v1/usage/export.csv", self.member_token,
                                      params=self.usage_dates())
        rows = list(csv.DictReader(StringIO(response.text)))
        require(rows and all(row["tenant_id"] == self.tenant_id and row["plan"] == "team"
                             for row in rows), "CSV must contain only this tenant's team plan rows")
        require(sum(int(row["tasks"]) for row in rows) == int(self.task_path is not None),
                "CSV task total differs from expected usage")
        return "exported tenant billing row"

    async def check_knowledge(self) -> str:
        uploaded = await self.request("POST", "/v1/knowledge/documents", self.admin_token,
                                      expected=(201,), body={"title": "Commercial readiness",
                                                            "text": self.tenant_id, "format": "text"})
        document_id = uploaded.json()["id"]
        hits = (await self.request("POST", "/v1/knowledge/search", self.member_token,
                                   body={"query": self.tenant_id})).json()
        require(any(hit["document_id"] == document_id and self.tenant_id in hit["text"]
                    and hit["citation"].startswith(f"{document_id}#chunk-") for hit in hits),
                "knowledge search did not return the uploaded document and citation")
        return "uploaded and retrieved a cited knowledge document"

    async def check_stripe(self) -> str:
        summary = (await self.request("GET", "/v1/tenant/billing", self.admin_token)).json()
        enabled = summary["enabled"]
        if enabled is False:
            raise CheckSkipped("Stripe is disabled on the API")
        require(enabled is True, "billing status did not report whether Stripe is enabled")
        checkout = await self.request("POST", "/v1/tenant/billing/checkout", self.admin_token,
                                      body={"plan": "team"})
        url = httpx.URL(checkout.json()["url"])
        require(url.scheme in {"https", "http"} and url.host, "missing checkout URL")
        if self.stripe_sim is not None:
            require(url.scheme == self.stripe_sim.scheme and url.netloc == self.stripe_sim.netloc
                    and not url.userinfo and re.fullmatch(r"/_sim/checkout/cs_[a-zA-Z0-9_-]+", url.path),
                    "checkout URL does not match --stripe-sim")
            self.stripe_checkout = url.path
        return "received a checkout URL"

    async def stripe_control(self, path: str) -> dict[str, Any]:
        assert self.stripe_sim is not None
        response = await self.http.post(self.stripe_sim.copy_with(path=path))
        require(response.status_code == 200, f"Stripe simulator returned HTTP {response.status_code}")
        return response.json()

    def stripe_subscription_path(self) -> str:
        if self.stripe_subscription is None:
            raise CheckSkipped("Stripe is disabled on the API")
        return f"/_sim/subscriptions/{self.stripe_subscription}"

    async def check_stripe_tenant(self, status: str) -> dict[str, Any]:
        tenant = (await self.request("GET", "/v1/tenant", self.admin_token)).json()
        require(tenant["id"] == self.tenant_id and tenant["plan"] == "team"
                and tenant["status"] == status, f"expected a {status} team tenant")
        billing = (await self.request("GET", "/v1/tenant/billing", self.admin_token)).json()
        require(billing["suspended_for_nonpayment"] is (status == "suspended"),
                "tenant suspension did not match Stripe payment state")
        return billing

    async def complete_stripe_checkout(self) -> str:
        if self.stripe_checkout is None:
            raise CheckSkipped("Stripe is disabled on the API")
        tenant = (await self.request("PATCH", self.tenant_path, self.operator_token,
                                     body={"plan": "free"})).json()
        require(tenant["plan"] == "free", "could not set the initial free plan")
        checkout = await self.stripe_control(f"{self.stripe_checkout}/complete")
        subscription = checkout["subscription"]
        require(isinstance(subscription, str) and re.fullmatch(r"sub_[a-zA-Z0-9_-]+", subscription),
                "completed checkout did not return a subscription")
        self.stripe_subscription = subscription
        billing = await self.check_stripe_tenant("active")
        require(billing["subscription_status"] == "active", "subscription did not become active")
        return "signed checkout activated the subscription and changed the plan to team"

    async def check_stripe_payment_failures(self) -> str:
        path = self.stripe_subscription_path()
        for attempt in range(1, 4):
            invoice = await self.stripe_control(f"{path}/payment_failed")
            self.stripe_invoice = self.stripe_invoice or invoice["id"]
            require(invoice["id"] == self.stripe_invoice and invoice["status"] == "open"
                    and invoice["attempt_count"] == attempt, "expected retries on the same invoice")
            await self.check_stripe_tenant("suspended" if attempt == 3 else "active")
        return "three failed payments suspended the tenant"

    async def check_stripe_suspension(self) -> str:
        self.stripe_subscription_path()
        return await self.check_suspension()

    async def check_stripe_payment(self) -> str:
        invoice = await self.stripe_control(f"{self.stripe_subscription_path()}/payment_succeeded")
        billing = await self.check_stripe_tenant("active")
        require(billing["subscription_status"] == "active", "subscription did not recover")
        require(invoice["id"] == self.stripe_invoice and invoice["status"] == "paid"
                and 0 < invoice["amount_paid"] == invoice["amount_due"], "expected the overdue invoice to be paid")
        require(any(row["id"] == self.stripe_invoice and row["status"] == "paid"
                    and row["subscription_id"] == self.stripe_subscription
                    and row["amount_paid"] == invoice["amount_paid"] for row in billing["invoices"]),
                "paid invoice missing from tenant billing")
        return "payment resumed the tenant and the paid invoice is listed"

    async def set_tenant_status(self, action: str, status: str) -> None:
        response = await self.request("POST", f"{self.tenant_path}/{action}", self.operator_token)
        tenant = response.json()
        require(tenant["id"] == self.tenant_id and tenant["status"] == status,
                f"tenant did not become {status}")

    async def suspend_tenant(self) -> str:
        await self.set_tenant_status("suspend", "suspended")
        return "tenant suspended"

    async def check_suspension(self) -> str:
        await self.expect_denied(self.member_token, "tenant_suspended")
        return "submission denied with 403 tenant_suspended"

    async def resume_tenant(self) -> str:
        await self.set_tenant_status("resume", "active")
        return "tenant active again"

    async def cleanup(self) -> str:
        try:
            if self.task_path and not self.task_terminal:
                await self.request("POST", f"{self.task_path}/cancel", self.admin_token,
                                   expected=(200, 409))
            if self.stripe_subscription is not None:
                await self.stripe_control(f"{self.stripe_subscription_path()}/cancel")
        finally:
            response = await self.request("DELETE", self.tenant_path, self.operator_token,
                                          expected=(204, 404, 405, 501))
            if response.status_code != 204:
                await self.set_tenant_status("suspend", "suspended")
        if response.status_code != 204:
            return "tenant suspended because DELETE is unavailable"
        return "tenant deleted"


async def run(
    api: httpx.URL, token: str, repo: str | None, wait_seconds: int, stripe_sim: httpx.URL | None = None,
) -> int:
    async with httpx.AsyncClient(base_url=api, timeout=30, trust_env=False) as http:
        return await CommercialSmoke(http, token, repo, wait_seconds, stripe_sim).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="SCA API base URL")
    parser.add_argument("--with-agent", action="store_true", help="run one verified coding task")
    parser.add_argument("--repo", help="bare repository path on the worker or Git clone URL")
    parser.add_argument("--stripe-sim", metavar="URL", help="run the signed billing flow against this simulator")
    parser.add_argument("--wait-seconds", type=int, default=600, help="deadline for each polling step")
    args = parser.parse_args(argv)
    if args.with_agent and not args.repo:
        parser.error("--with-agent requires --repo")
    if args.wait_seconds <= 0:
        parser.error("--wait-seconds must be positive")
    token = os.environ.get("SCA_PLATFORM_ADMIN_TOKEN", "").strip()
    if not token:
        parser.error("set SCA_PLATFORM_ADMIN_TOKEN in the environment")
    try:
        api = httpx.URL(args.api)
    except httpx.InvalidURL:
        parser.error("--api requires an HTTP(S) base URL")
    if api.scheme not in {"http", "https"} or not api.host or api.userinfo:
        parser.error("--api requires an HTTP(S) base URL without credentials")
    stripe_sim = None
    if args.stripe_sim is not None:
        try:
            stripe_sim = httpx.URL(args.stripe_sim)
        except httpx.InvalidURL:
            parser.error("--stripe-sim requires an HTTP(S) origin")
        if (stripe_sim.scheme not in {"http", "https"} or not stripe_sim.host or stripe_sim.userinfo
                or stripe_sim.raw_path != b"/" or stripe_sim.fragment):
            parser.error("--stripe-sim requires an HTTP(S) origin without credentials")
    return asyncio.run(run(api, token, args.repo if args.with_agent else None, args.wait_seconds, stripe_sim))


if __name__ == "__main__":
    raise SystemExit(main())
