"""Source control operations performed by the API and worker, outside the sandbox."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx

from sca.config import Settings
from sca.core.errors import NotFoundError, WorkspaceError
from sca.core.interfaces import SecretProvider
from sca.core.models import TERMINAL_STATES, Task, TaskState
from sca.db.repo import Store
from sca.kinds.coding.spec import PullRequestSpec
from sca.scm.models import Issue, PullRequest
from sca.scm.provider import (
    GiteaProvider,
    GitHubProvider,
    NewPullRequest,
    RemotePullRequest,
    ScmError,
    ScmProvider,
)
from sca.scm.repository import identify_repository


class ScmService:
    def __init__(
        self, settings: Settings, secrets: SecretProvider, *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.transport = transport

    @asynccontextmanager
    async def _provider(self, task: Task) -> AsyncIterator[ScmProvider]:
        try:
            repo = identify_repository(self.settings, task.spec["repo_url"])
        except ValueError:
            raise ScmError("任务仓库地址不属于已配置的 GitHub 或 Gitea") from None
        try:
            credential = await self.secrets.repo_credential(task.tenant_id, repo.clone_url)
        except WorkspaceError:
            raise ScmError("无法获取 SCM 仓库凭证") from None
        if credential is None or not credential.password:
            raise ScmError("SCM 仓库缺少 API 凭证")
        async with httpx.AsyncClient(
            base_url=repo.api_url, timeout=10, transport=self.transport,
        ) as client:
            provider = GitHubProvider if repo.provider == "github" else GiteaProvider
            yield provider(client, repo, credential.password)

    def _portal_link(self, task: Task) -> str:
        portal = self.settings.scm_portal_url or self.settings.api_url.rstrip("/") + "/portal"
        return f"[查看 Portal 任务]({str(portal).rstrip('/')}#/tasks/{task.id})"

    @staticmethod
    def _base(task: Task, config: PullRequestSpec) -> str:
        return (config.base or task.spec.get("base_ref", "main")).removeprefix("refs/heads/")

    async def _evidence(self, store: Store, task: Task) -> str:
        result = task.result
        checks = await store.scm_check_results(task.id, result["turns"])
        summary = [f"验证结果：{'通过' if result['verification_passed'] else '未通过'}"]
        local = checks.get("verification.completed", {})
        if local.get("skipped"):
            summary.append("命令检查已跳过")
        for index, check in enumerate(local.get("checks", []), start=1):
            outcome = "通过" if check["passed"] else "超时" if check["timed_out"] else "未通过"
            summary.append(f"命令检查 {index}：{outcome}（退出码 {check['exit_code']}）")
        if ci := checks.get("ci.completed"):
            summary.append(f"外部 CI：{'通过' if ci['passed'] else '未通过'}")
        if "acceptance_passed" in result:
            summary.append(f"验收检查：{'通过' if result['acceptance_passed'] else '未通过'}")
        cost = task.usage.get("cost_micro_usd")
        cost_text = f"${cost / 1_000_000:.6f} USD" if cost is not None else "未记录"
        summary.append(f"模型费用：{cost_text}")
        return "\n".join(f"- {line}" for line in summary)

    async def _publish_pr(
        self, provider: ScmProvider, store: Store, task: Task, config: PullRequestSpec,
    ) -> RemotePullRequest:
        head, base = task.result["branch"], self._base(task, config)
        start, end = f"<!-- sca:pr:{task.id} -->", f"<!-- /sca:pr:{task.id} -->"
        instruction = task.spec["instruction"]
        evidence = await self._evidence(store, task)
        issue = task.result.get("issue")
        link = f"\n\n关联工单 #{issue['number']}" if issue else ""
        section = (
            f"{start}\n## SCA 任务\n\n{self._portal_link(task)}{link}\n\n{evidence}"
            f"\n\n### 任务指令\n\n{instruction[:32000]}\n{end}"
        )
        stored = task.result.get("pr")
        if stored:
            pull = await provider.get_pull_request(stored["number"])
        else:
            pull = await provider.find_pull_request(head, base, start)
        if pull is None:
            title = config.title or (instruction.strip().split("\n", 1)[0][:250] or "SCA 任务")
            body = "\n\n".join(part for part in (config.body, section) if part)
            pull = await provider.open_pull_request(
                NewPullRequest(title=title, body=body, head=head, base=base, draft=config.draft),
            )
        elif pull.ref.state == "open" and pull.matches(provider.repo, head, base):
            before, start_found, rest = pull.body.partition(start)
            _, end_found, after = rest.partition(end)
            if start_found and end_found:
                body = f"{before}{section}{after}"
                if body != pull.body:
                    await provider.update_pull_request_body(pull.ref.number, body)
        await store.record_scm_pull_request(task.id, pull.ref.model_dump(), opened=not stored)
        return pull

    async def open_for_review(self, store: Store, task: Task) -> None:
        pr = task.spec.get("pull_request")
        if not task.result.get("pushed") or pr is None or pr.get("open_on", "review") != "review":
            return
        async with self._provider(task) as provider:
            await self._publish_pr(provider, store, task, PullRequestSpec.model_validate(pr))

    async def record_review(
        self, store: Store, task: Task, decision: Literal["approve", "reject"],
    ) -> None:
        config = task.spec.get("pull_request")
        if config is None or task.spec.get("auto_finish"):
            return
        stored = task.result.get("pr")
        if stored is None and (decision == "reject" or not task.result.get("pushed")):
            return
        spec = PullRequestSpec.model_validate(config)
        async with self._provider(task) as provider:
            if stored:
                pull = await provider.get_pull_request(stored["number"])
            else:
                pull = await self._publish_pr(provider, store, task, spec)
            decision_text = "通过" if decision == "approve" else "拒绝"
            await provider.comment(
                pull.ref.number, f"SCA 审核{decision_text}。\n\n{self._portal_link(task)}",
                f"<!-- sca:review:{task.id}:{decision} -->",
            )
            pr = pull.ref
            if decision == "approve" and spec.merge_on_approve and pr.state != "merged":
                commit = task.result.get("commit")
                matches = pull.matches(provider.repo, task.result["branch"], self._base(task, spec))
                if not commit or not task.result.get("pushed") or not matches:
                    raise ScmError("PR 与已审核的任务分支不匹配，无法合并")
                await provider.merge_pull_request(pr.number, commit)
                pr = pr.model_copy(update={"state": "merged"})
            await store.record_scm_pull_request(task.id, pr.model_dump())

    async def comment_outcome(self, task: Task) -> None:
        if task.state not in TERMINAL_STATES or "issue" not in task.result:
            return
        issue = Issue.model_validate(task.result["issue"])
        outcome = {
            TaskState.SUCCEEDED: "成功", TaskState.FAILED: "失败", TaskState.CANCELLED: "已取消",
        }[task.state]
        body = f"SCA 任务{outcome}（`{task.state}`）。\n\n{self._portal_link(task)}"
        if pr := task.result.get("pr"):
            body += f"\n\n[查看 PR #{pr['number']}]({pr['url']})"
        async with self._provider(task) as provider:
            await provider.comment(issue.number, body, f"<!-- sca:outcome:{task.id} -->")

    async def refresh_pr(self, store: Store, task: Task) -> PullRequest:
        if not (pr := task.result.get("pr")):
            raise NotFoundError("任务尚未创建 PR")
        async with self._provider(task) as provider:
            remote = await provider.get_pull_request(pr["number"])
        saved = await store.record_scm_pull_request(task.id, remote.ref.model_dump())
        return PullRequest.model_validate(saved)
