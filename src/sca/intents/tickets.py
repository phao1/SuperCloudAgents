"""Import and update tickets through Jira Cloud, Linear and GitHub's official APIs."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import unescape
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sca.core.errors import WorkspaceError
from sca.core.models import Task
from sca.intents.base import IntentBundle, IntentSource
from sca.intents.remote import RemoteClient, RemoteError, SecretName, https_url

_PAGE_SIZE = 50
_PAGE_LIMIT = 4
_ATTACHMENT_LIMIT = 12
_LINEAR_API = "https://api.linear.app/graphql"
_URL = re.compile(r"""https://[^\s<>"')\]]+""")
_ACCEPTANCE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]+|\*\*)?"
    r"(?:acceptance criteria|验收标准|验收条件)[ \t]*[:：]?(?:\*\*)?[ \t]*\n"
)


class TicketParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    writeback: bool = True


class JiraParams(TicketParams):
    site: str = Field(max_length=2048)
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}-[0-9]{1,18}$")
    token_secret: SecretName
    acceptance_field: str | None = Field(default=None, pattern=r"^customfield_[0-9]+$")
    transition_id: str | None = Field(
        default=None, pattern=r"^[0-9]{1,20}$", coerce_numbers_to_str=True
    )


class LinearParams(TicketParams):
    issue_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")
    token_secret: SecretName
    state_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")


class GitHubParams(TicketParams):
    repo: str = Field(pattern=r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
    number: int = Field(gt=0)

    @field_validator("repo")
    @classmethod
    def _normalize_repo(cls, repo: str) -> str:
        if repo.split("/")[1] in {".", ".."}:
            raise ValueError("invalid GitHub repository")
        return repo.lower()


@dataclass(frozen=True)
class Attachment:
    title: str
    url: str


@dataclass
class Ticket:
    title: str
    description: str
    comments: list[str]
    attachments: list[Attachment] = field(default_factory=list)
    acceptance: str = ""


@dataclass(frozen=True)
class Outcome:
    summary: str
    pr_url: str | None = None

    @classmethod
    def from_task(cls, task: Task) -> "Outcome":
        result = task.result
        pr = result.get("pull_request")
        url = result.get("pr_url") or result.get("pull_request_url")
        if not url and isinstance(pr, dict):
            url = pr.get("html_url") or pr.get("url")
        elif not url and isinstance(pr, str):
            url = pr
        try:
            link = httpx.URL(url) if isinstance(url, str) else None
        except httpx.InvalidURL:
            link = None
        if link and (link.scheme not in {"http", "https"} or not link.host or link.userinfo):
            link = None
        return cls(
            f"SCA task {task.id} finished with state {task.state}.",
            str(link.copy_with(query=None)) if link else None,
        )

    def markdown(self) -> str:
        return self.summary + (f"\n\n[Pull request](<{self.pr_url}>)" if self.pr_url else "")

    def adf(self) -> dict:
        text: list[dict] = [{"type": "text", "text": self.summary}]
        if self.pr_url:
            text.extend(
                [
                    {"type": "hardBreak"},
                    {
                        "type": "text",
                        "text": "Pull request",
                        "marks": [{"type": "link", "attrs": {"href": self.pr_url}}],
                    },
                ]
            )
        return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": text}]}


class TicketClient(Protocol):
    params: TicketParams

    async def fetch(self) -> Ticket: ...
    async def download(self, attachment: Attachment) -> bytes | None: ...
    async def comment(self, outcome: Outcome) -> None: ...
    async def transition(self) -> bool: ...


def _adf_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    node_type = value.get("type")
    if node_type == "text":
        return value.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    content = "".join(_adf_text(child) for child in value.get("content", []))
    if node_type == "heading":
        return "\n## " + content + "\n"
    if node_type in {"paragraph", "listItem", "tableRow", "codeBlock"}:
        return content + "\n"
    return content


class JiraClient:
    def __init__(self, params: JiraParams, remote: RemoteClient):
        self.params = params
        self.remote = remote
        credential = remote.credential("jira", params.token_secret)
        assert credential is not None
        site = https_url(params.site)
        if (
            site is None
            or site.path != "/"
            or site.query
            or site.fragment
            or site != https_url(credential.site or "")
        ):
            raise RemoteError("jira", "site is not configured for this credential")
        if not credential.email:
            raise RemoteError("jira", "credential needs an Atlassian account email")
        self.base = str(site).rstrip("/") + "/rest/api/3"
        self.issue = f"{self.base}/issue/{params.issue_key}"
        self.auth = httpx.BasicAuth(credential.email, credential.token.get_secret_value())

    async def fetch(self) -> Ticket:
        fields = ["summary", "description", "attachment"]
        if self.params.acceptance_field:
            fields.append(self.params.acceptance_field)
        data = await self.remote.json(
            "jira", "GET", self.issue, auth=self.auth, params={"fields": ",".join(fields)}
        )
        values = data["fields"]
        comments = []
        start = 0
        for _ in range(_PAGE_LIMIT):
            page = await self.remote.json(
                "jira",
                "GET",
                self.issue + "/comment",
                auth=self.auth,
                params={"maxResults": _PAGE_SIZE, "startAt": start},
            )
            entries = page.get("comments", [])
            comments.extend(_adf_text(entry.get("body")).strip() for entry in entries)
            start += len(entries)
            if not entries or start >= page.get("total", start):
                break
        attachments = [
            Attachment(item.get("filename") or "attachment", str(item["id"]))
            for item in values.get("attachment") or []
            if re.fullmatch(r"\d{1,20}", str(item.get("id", "")))
        ]
        return Ticket(
            values["summary"],
            _adf_text(values.get("description")).strip(),
            comments,
            attachments,
            _adf_text(values.get(self.params.acceptance_field or "")).strip(),
        )

    async def download(self, attachment: Attachment) -> bytes:
        url = httpx.URL(f"{self.base}/attachment/content/{attachment.url}?redirect=false")
        return await self.remote.asset(
            "jira",
            str(url),
            lambda target: (target.host, target.path) == (url.host, url.path),
            auth=self.auth,
        )

    async def comment(self, outcome: Outcome) -> None:
        await self.remote.json(
            "jira", "POST", self.issue + "/comment", auth=self.auth, json={"body": outcome.adf()}
        )

    async def transition(self) -> bool:
        if not self.params.transition_id:
            return False
        await self.remote.json(
            "jira",
            "POST",
            self.issue + "/transitions",
            auth=self.auth,
            json={"transition": {"id": self.params.transition_id}},
        )
        return True


_LINEAR_CONNECTIONS = {"comments": "body", "attachments": "title url"}


def _linear_selection(connection: str, after: str = "") -> str:
    return (
        f"{connection}(first: {_PAGE_SIZE}{after}) {{ "
        f"nodes {{ {_LINEAR_CONNECTIONS[connection]} }} pageInfo {{ hasNextPage endCursor }} }}"
    )


def _linear_asset(url: httpx.URL) -> bool:
    return url.host == "uploads.linear.app"


class LinearClient:
    def __init__(self, params: LinearParams, remote: RemoteClient):
        self.params = params
        self.remote = remote
        credential = remote.credential("linear", params.token_secret)
        assert credential is not None
        self.headers = {"Authorization": credential.token.get_secret_value()}
        self.issue_uuid: str | None = None

    async def _graphql(self, query: str, **variables: Any) -> dict:
        response = await self.remote.json(
            "linear",
            "POST",
            _LINEAR_API,
            headers=self.headers,
            json={"query": query, "variables": variables},
        )
        if response.get("errors") or not isinstance(response.get("data"), dict):
            raise RemoteError("linear", "GraphQL operation failed")
        return response["data"]

    async def _issue_id(self) -> str:
        if self.issue_uuid:
            return self.issue_uuid
        try:
            self.issue_uuid = str(UUID(self.params.issue_id))
        except ValueError:
            data = await self._graphql(
                "query IntentIssueId($id: String!) { issue(id: $id) { id } }",
                id=self.params.issue_id,
            )
            if not (issue := data.get("issue")):
                raise RemoteError("linear", "issue was not found")
            self.issue_uuid = issue["id"]
        return self.issue_uuid

    async def _entries(self, issue: dict, connection: str) -> list[dict]:
        page = issue[connection]
        entries = list(page["nodes"])
        for _ in range(_PAGE_LIMIT - 1):
            info = page["pageInfo"]
            if not info["hasNextPage"] or not info["endCursor"]:
                break
            selection = _linear_selection(connection, ", after: $after")
            data = await self._graphql(
                f"query IntentPage($id: String!, $after: String!) "
                f"{{ issue(id: $id) {{ {selection} }} }}",
                id=issue["id"],
                after=info["endCursor"],
            )
            page = data["issue"][connection]
            entries.extend(page["nodes"])
        return entries

    async def fetch(self) -> Ticket:
        connections = " ".join(_linear_selection(name) for name in _LINEAR_CONNECTIONS)
        data = await self._graphql(
            "query IntentIssue($id: String!) { issue(id: $id) { "
            f"id title description {connections} }} }}",
            id=self.params.issue_id,
        )
        if not (issue := data.get("issue")):
            raise RemoteError("linear", "issue was not found")
        self.issue_uuid = issue["id"]
        description = issue.get("description") or ""
        comments = [entry["body"] for entry in await self._entries(issue, "comments")]
        attachments = [
            Attachment(entry.get("title") or "attachment", entry["url"])
            for entry in await self._entries(issue, "attachments")
        ]
        attachments.extend(_inline_attachments([description, *comments], _linear_asset))
        return Ticket(issue["title"], description, comments, attachments)

    async def download(self, attachment: Attachment) -> bytes | None:
        url = https_url(attachment.url)
        if url and _linear_asset(url):
            return await self.remote.asset(
                "linear", attachment.url, _linear_asset, headers=self.headers
            )
        return None

    async def comment(self, outcome: Outcome) -> None:
        data = await self._graphql(
            "mutation IntentComment($input: CommentCreateInput!) "
            "{ commentCreate(input: $input) { success } }",
            input={"issueId": await self._issue_id(), "body": outcome.markdown()},
        )
        if not data["commentCreate"]["success"]:
            raise RemoteError("linear", "comment was not created")

    async def transition(self) -> bool:
        if not self.params.state_id:
            return False
        data = await self._graphql(
            "mutation IntentTransition($id: String!, $input: IssueUpdateInput!) "
            "{ issueUpdate(id: $id, input: $input) { success } }",
            id=await self._issue_id(),
            input={"stateId": self.params.state_id},
        )
        if not data["issueUpdate"]["success"]:
            raise RemoteError("linear", "issue was not transitioned")
        return True


def _github_asset(url: httpx.URL) -> bool:
    if url.host == "github.com":
        return url.path.startswith("/user-attachments/")
    return url.host in {
        "user-images.githubusercontent.com",
        "private-user-images.githubusercontent.com",
        "objects.githubusercontent.com",
    }


class GitHubClient:
    def __init__(self, params: GitHubParams, remote: RemoteClient):
        self.params = params
        self.remote = remote
        self.issue = f"https://api.github.com/repos/{params.repo}/issues/{params.number}"

    async def _headers(self, *, write: bool = False) -> dict[str, str]:
        media_type = "application/vnd.github+json" if write else "application/vnd.github.full+json"
        headers = {"Accept": media_type}
        credential = self.remote.credential("github_issue", self.params.repo, required=False)
        token = credential.token.get_secret_value() if credential else ""
        if not token and self.remote.secrets:
            try:
                repo = await self.remote.secrets.repo_credential(
                    self.remote.tenant_id, f"https://github.com/{self.params.repo}"
                )
            except WorkspaceError:
                raise RemoteError("github_issue", "repository credential lookup failed") from None
            if repo:
                token = repo.password
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif write:
            raise RemoteError("github_issue", "write-back needs a repository credential")
        return headers

    async def fetch(self) -> Ticket:
        headers = await self._headers()
        data = await self.remote.json("github_issue", "GET", self.issue, headers=headers)
        if "pull_request" in data:
            raise RemoteError("github_issue", "the number refers to a pull request")
        description = data.get("body") or ""
        comments = []
        rendered = [data.get("body_html") or ""]
        for page in range(1, _PAGE_LIMIT + 1):
            items = await self.remote.json(
                "github_issue",
                "GET",
                self.issue + "/comments",
                headers=headers,
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            comments.extend(item.get("body") or "" for item in items)
            rendered.extend(item.get("body_html") or "" for item in items)
            if len(items) < _PAGE_SIZE:
                break
        return Ticket(
            data["title"],
            description,
            comments,
            _inline_attachments([*rendered, description, *comments], _github_asset),
        )

    async def download(self, attachment: Attachment) -> bytes:
        return await self.remote.asset("github_issue", attachment.url, _github_asset)

    async def comment(self, outcome: Outcome) -> None:
        await self.remote.json(
            "github_issue",
            "POST",
            self.issue + "/comments",
            headers=await self._headers(write=True),
            json={"body": outcome.markdown()},
        )

    async def transition(self) -> bool:
        return False


def ticket_client(source: IntentSource, remote: RemoteClient) -> TicketClient:
    if source.type == "jira":
        return JiraClient(JiraParams.model_validate(source.params), remote)
    if source.type == "linear":
        return LinearClient(LinearParams.model_validate(source.params), remote)
    if source.type == "github_issue":
        return GitHubClient(GitHubParams.model_validate(source.params), remote)
    raise ValueError("unknown ticket intent")


def _inline_attachments(texts: list[str], allowed: Callable[[httpx.URL], bool]) -> list[Attachment]:
    urls = dict.fromkeys(unescape(match[0]) for text in texts for match in _URL.finditer(text))
    return [
        Attachment(PurePosixPath(url.path).name or "image", link)
        for link in urls
        if (url := https_url(link)) and allowed(url)
    ]


def _notes_text(text: str) -> str:
    def remove_signature(match: re.Match[str]) -> str:
        url = https_url(match[0])
        if url and (_github_asset(url) or _linear_asset(url)):
            return str(url.copy_with(query=None, fragment=None))
        return match[0]

    return _URL.sub(remove_signature, text)


def _attachment_extension(title: str, data: bytes) -> str:
    for signature, extension in (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
    ):
        if data.startswith(signature):
            return extension
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    extension = PurePosixPath(title).suffix.lower()
    if extension in {".pdf", ".txt", ".md", ".csv", ".json", ".log", ".zip"}:
        return extension
    return ".bin"


async def compile_ticket(source: IntentSource, remote: RemoteClient) -> IntentBundle:
    client = ticket_client(source, remote)
    ticket = await client.fetch()
    acceptance = ticket.acceptance
    if not acceptance and (heading := _ACCEPTANCE.search(ticket.description)):
        acceptance = re.split(r"(?m)^#{1,6}\s+", ticket.description[heading.end() :], maxsplit=1)[0]
    notes = [
        f"# {_notes_text(ticket.title[:512])}",
        "",
        "## Description",
        "",
        _notes_text(ticket.description[:24000]),
    ]
    if acceptance:
        notes.extend(["", "## Acceptance criteria", "", _notes_text(acceptance[:8000])])
    if ticket.comments:
        notes.extend(["", "## Comments", ""])
        comments = "\n\n---\n\n".join(ticket.comments)[:32000]
        notes.append(_notes_text(comments))
    files: dict[str, bytes] = {}
    if ticket.attachments:
        notes.extend(["", "## Attachments", ""])
        attachments = {attachment.url: attachment for attachment in ticket.attachments}
        for index, attachment in enumerate(attachments.values(), 1):
            label = _notes_text(" ".join(attachment.title[:256].splitlines()))
            if index > _ATTACHMENT_LIMIT:
                notes.append(f"- {len(attachments) - _ATTACHMENT_LIMIT} more attachments")
                break
            try:
                data = await client.download(attachment)
            except RemoteError:
                notes.append(f"- {label} (download unavailable)")
                continue
            if data is None:
                url = https_url(attachment.url)
                location = str(url.copy_with(query=None, fragment=None)) if url else "external link"
                notes.append(f"- {label}: {location}")
                continue
            extension = _attachment_extension(attachment.title, data)
            filename = f"{source.name}-attachment-{index}{extension}"
            files[filename] = data
            notes.append(f"- {label}: {filename}")
    filename = source.name + "-ticket.md"
    files[filename] = ("\n".join(notes) + "\n").encode()
    return IntentBundle(
        context_files=files,
        prompt_notes=[f"Ticket requirements and references: .sca/context/{filename}."],
    )
