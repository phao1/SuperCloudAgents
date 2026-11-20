from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import AnyHttpUrl, BaseModel, Field, ValidationError

from sca.scm.models import PullRequest
from sca.scm.repository import Repository


class ScmError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class NewPullRequest:
    title: str
    body: str
    head: str
    base: str
    draft: bool


class _HeadRepo(BaseModel):
    full_name: str


class _Branch(BaseModel):
    ref: str
    label: str | None = None
    repo: _HeadRepo | None = None


class _PullResponse(BaseModel):
    number: int = Field(gt=0, strict=True)
    html_url: AnyHttpUrl
    state: Literal["open", "closed"]
    merged: bool = False
    merged_at: str | None = None
    body: str | None = None
    head: _Branch | None = None
    base: _Branch | None = None


@dataclass(frozen=True)
class RemotePullRequest:
    ref: PullRequest
    body: str
    head: str | None
    base: str | None
    head_repo: str | None

    def matches(self, repo: Repository, head: str, base: str) -> bool:
        return (
            self.head == head and self.base == base
            and self.head_repo is not None and self.head_repo.lower() == repo.full_name.lower()
        )


class ScmProvider(ABC):
    authorization_scheme: str
    accept: str = "application/json"
    page_size_param: str = "limit"

    def __init__(self, client: httpx.AsyncClient, repo: Repository, token: str) -> None:
        self.client = client
        self.repo = repo
        self._headers = {
            "Authorization": f"{self.authorization_scheme} {token}",
            "Accept": self.accept,
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(
                method, f"repos/{self.repo.full_name}/{path}", headers=self._headers,
                follow_redirects=False, **kwargs,
            )
        except httpx.RequestError:
            raise ScmError(f"无法连接 {self.repo.provider} API") from None
        if not response.is_success:
            # Error responses can include request details. Keep them out of task events/history.
            raise ScmError(
                f"{self.repo.provider} API 返回 HTTP {response.status_code}",
                status=response.status_code,
            )
        return response

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise ScmError(f"{self.repo.provider} API 返回无效的 JSON") from None

    def _pull(self, data: Any) -> RemotePullRequest:
        try:
            pull = _PullResponse.model_validate(data)
        except ValidationError:
            raise ScmError(f"{self.repo.provider} API 返回无效的 PR 数据") from None
        head = pull.head.ref if pull.head else None
        if self.repo.provider == "gitea" and pull.head and pull.head.label:
            # After branch deletion Gitea leaves the source branch name in label.
            head = pull.head.label
        return RemotePullRequest(
            ref=PullRequest(
                provider=self.repo.provider, number=pull.number, url=str(pull.html_url),
                state="merged" if pull.merged or pull.merged_at else pull.state,
            ),
            body=pull.body or "",
            head=head,
            base=pull.base.ref if pull.base else None,
            head_repo=pull.head.repo.full_name if pull.head and pull.head.repo else None,
        )

    async def _pages(self, path: str, **filters: str) -> AsyncIterator[dict]:
        page = 1
        while True:
            response = await self._request(
                "GET", path, params={**filters, self.page_size_param: 50, "page": page},
            )
            data = self._json(response)
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise ScmError(f"{self.repo.provider} API 返回无效的列表")
            for row in data:
                yield row
            if "next" not in response.links:
                break
            page += 1

    def _pull_filters(self, head: str, base: str) -> dict[str, str]:
        return {"state": "all"}

    async def find_pull_request(self, head: str, base: str, marker: str) -> RemotePullRequest | None:
        async for row in self._pages("pulls", **self._pull_filters(head, base)):
            if not isinstance(row.get("body"), str) or marker not in row["body"]:
                continue
            pull = self._pull(row)
            if pull.matches(self.repo, head, base):
                return pull
        return None

    async def get_pull_request(self, number: int) -> RemotePullRequest:
        pull = self._pull(self._json(await self._request("GET", f"pulls/{number}")))
        if pull.ref.number != number:
            raise ScmError(f"{self.repo.provider} API 返回的 PR 编号不匹配")
        return pull

    async def update_pull_request_body(self, number: int, body: str) -> None:
        await self._request("PATCH", f"pulls/{number}", json={"body": body})

    async def comment(self, number: int, body: str, marker: str) -> None:
        # A retry can follow a successful POST whose response was lost.
        path = f"issues/{number}/comments"
        async for row in self._pages(path):
            if isinstance(row.get("body"), str) and row["body"].endswith(marker):
                return
        await self._request("POST", path, json={"body": f"{body}\n\n{marker}"})

    @abstractmethod
    async def open_pull_request(self, proposal: NewPullRequest) -> RemotePullRequest: ...

    @abstractmethod
    async def merge_pull_request(self, number: int, commit: str) -> None: ...


class GitHubProvider(ScmProvider):
    authorization_scheme = "Bearer"
    accept = "application/vnd.github+json"
    page_size_param = "per_page"

    def _pull_filters(self, head: str, base: str) -> dict[str, str]:
        owner = self.repo.full_name.split("/", 1)[0]
        return {"state": "all", "head": f"{owner}:{head}", "base": base}

    async def open_pull_request(self, proposal: NewPullRequest) -> RemotePullRequest:
        response = await self._request("POST", "pulls", json={
            "title": proposal.title, "body": proposal.body, "head": proposal.head,
            "base": proposal.base, "draft": proposal.draft,
        })
        return self._pull(self._json(response))

    async def merge_pull_request(self, number: int, commit: str) -> None:
        response = await self._request("PUT", f"pulls/{number}/merge", json={"sha": commit})
        data = self._json(response)
        if not isinstance(data, dict) or data.get("merged") is not True:
            raise ScmError("GitHub 未完成 PR 合并")


class GiteaProvider(ScmProvider):
    authorization_scheme = "token"

    async def open_pull_request(self, proposal: NewPullRequest) -> RemotePullRequest:
        title = proposal.title
        if proposal.draft and not title.lower().startswith(("wip:", "[wip]")):
            title = f"WIP: {title}"
        response = await self._request("POST", "pulls", json={
            "title": title, "body": proposal.body, "head": proposal.head, "base": proposal.base,
        })
        return self._pull(self._json(response))

    async def merge_pull_request(self, number: int, commit: str) -> None:
        await self._request(
            "POST", f"pulls/{number}/merge", json={"Do": "merge", "head_commit_id": commit},
        )
