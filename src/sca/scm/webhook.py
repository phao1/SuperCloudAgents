"""Authenticate webhook deliveries and normalize issue triggers."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from sca.config import Settings
from sca.kinds.coding.spec import CodingSpec
from sca.scm.models import Issue, IssueDefaults
from sca.scm.repository import ProviderName, Repository, identify_repository

SIGNATURE_HEADERS = {"github": "X-Hub-Signature-256", "gitea": "X-Gitea-Signature"}


def signed_secret_ref(
    settings: Settings, provider: ProviderName, body: bytes, signature: str,
) -> tuple[str, str] | None:
    if provider == "github":
        if not signature.startswith("sha256="):
            return None
        signature = signature.removeprefix("sha256=")
    if not re.fullmatch(r"[a-f0-9]{64}", signature):
        return None

    matches = []
    # Routing starts with a verified tenant and then uses the same RLS role as authenticated APIs.
    for tenant, secrets in settings.scm_webhook_secrets.items():
        for ref, secret in secrets.items():
            value = secret.get_secret_value()
            if not value:
                continue
            digest = hmac.digest(value.encode(), body, "sha256").hex()
            if hmac.compare_digest(digest, signature):
                matches.append((tenant, ref))
    return matches[0] if len(matches) == 1 else None


class _Label(BaseModel):
    name: str


class _Issue(BaseModel):
    number: int = Field(gt=0, strict=True)
    title: str = Field(min_length=1)
    body: str | None = None
    labels: list[_Label] | None = None
    pull_request: dict | None = None


class _Repository(BaseModel):
    full_name: str
    clone_url: str
    default_branch: str = "main"


class _Comment(BaseModel):
    id: int = Field(gt=0, strict=True)
    body: str


class _Payload(BaseModel):
    repository: _Repository
    issue: _Issue
    label: _Label | None = None
    comment: _Comment | None = None
    is_pull: bool = False


@dataclass(frozen=True)
class IssueEvent:
    repository: Repository
    issue: Issue
    title: str
    text: str | None
    default_branch: str
    action: Literal["opened", "labeled", "comment"]
    labels: frozenset[str]
    comment_id: int | None = None
    comment_instruction: str | None = None

    def matches(self, defaults: IssueDefaults) -> bool:
        wanted = {label.casefold() for label in defaults.trigger_labels}
        if self.action == "comment" or (self.action == "opened" and not wanted):
            return True
        return not self.labels.isdisjoint(wanted)

    def task_spec(self, defaults: IssueDefaults) -> CodingSpec:
        instruction = "\n\n".join(part for part in (
            self.title, self.text, self.comment_instruction,
        ) if part)
        return CodingSpec(
            **defaults.model_dump(exclude={"trigger_labels", "base_ref"}),
            repo_url=self.repository.clone_url,
            instruction=instruction,
            base_ref=defaults.base_ref or defaults.pull_request.base or self.default_branch,
            push=True,
        )


def parse_issue_event(
    settings: Settings, provider: ProviderName, event: str, body: bytes,
) -> IssueEvent | None:
    if event not in {"issues", "issue_label", "issue_comment"}:
        return None
    data = json.loads(body)
    if not isinstance(data, dict):
        raise TypeError("Invalid webhook payload")

    action = data.get("action")
    opened = event == "issues" and action == "opened"
    labeled = (event == "issues" and action == "labeled") or (
        provider == "gitea" and event in {"issues", "issue_label"} and action == "label_updated"
    )
    comment_created = event == "issue_comment" and action == "created"
    if not (opened or labeled or comment_created):
        return None
    payload = _Payload.model_validate(data)
    if payload.is_pull or payload.issue.pull_request is not None:
        return None

    comment_id = None
    comment_instruction = None
    if comment_created:
        if payload.comment is None:
            raise ValueError("Missing issue comment")
        command = re.match(r"^\s*/sca[ \t]+run(?=\s|$)", payload.comment.body)
        if not command:
            return None
        comment_id = payload.comment.id
        comment_instruction = payload.comment.body[command.end():].strip()

    repo = identify_repository(settings, payload.repository.clone_url)
    if repo.provider != provider or repo.full_name.lower() != payload.repository.full_name.lower():
        raise ValueError("Issue repository does not match its clone URL")

    labels = payload.issue.labels or []
    if labeled and provider == "github":
        if payload.label is None:
            raise ValueError("Missing issue label")
        labels = [payload.label]
    number = payload.issue.number
    return IssueEvent(
        repository=repo,
        issue=Issue(provider=provider, number=number, url=f"{repo.web_url}/issues/{number}"),
        title=payload.issue.title,
        text=payload.issue.body,
        default_branch=payload.repository.default_branch,
        action="comment" if comment_created else "opened" if opened else "labeled",
        labels=frozenset(label.name.casefold() for label in labels),
        comment_id=comment_id,
        comment_instruction=comment_instruction,
    )
