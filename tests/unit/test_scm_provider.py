import json
from secrets import token_urlsafe

import httpx
import pytest
from pydantic import ValidationError

from sca.config import Settings
from sca.kinds.coding.spec import CodingSpec
from sca.scm.provider import GiteaProvider, GitHubProvider, NewPullRequest, ScmError
from sca.scm.repository import identify_repository


@pytest.mark.parametrize("provider,remote,prefix,scheme,merge_method,merge_body", [
    (GitHubProvider, "https://github.com/acme/app.git", "/github", "Bearer", "PUT", {"sha": "abc"}),
    (GiteaProvider, "http://forge.test:3000/acme/app.git", "/gitea/api/v1", "token", "POST",
     {"Do": "merge", "head_commit_id": "abc"}),
])
async def test_provider_requests(provider, remote, prefix, scheme, merge_method, merge_body):
    settings = Settings(
        _env_file=None, scm_github_api_url="https://scm.test/github",
        scm_gitea_hosts={"forge.test:3000": "https://scm.test/gitea/api/v1"},
    )
    repo = identify_repository(settings, remote)
    token = token_urlsafe()
    proposal = NewPullRequest("Fix", "<!-- test -->", "sca/1234", "release", True)
    row = {
        "number": 7, "html_url": "https://forge.test/acme/app/pulls/7", "state": "open",
        "head": {"ref": proposal.head, "repo": {"full_name": "Acme/App"}},
        "base": {"ref": proposal.base}, "body": proposal.body,
    }
    if provider is GiteaProvider:
        row["head"].update(ref="refs/pull/7/head", label=proposal.head)
    calls = []
    comments = []

    def handler(request):
        assert request.url.host == "scm.test"
        assert request.headers["authorization"].split()[0] == scheme
        assert request.headers["authorization"].split()[1] == token
        path = request.url.path.removeprefix(f"{prefix}/repos/acme/app/")
        calls.append((request.method, path))
        if request.method == "GET" and path == "pulls":
            assert request.url.params["state"] == "all"
            if provider is GitHubProvider:
                assert request.url.params["head"] == "acme:sca/1234"
                assert request.url.params["base"] == "release"
            if request.url.params["page"] == "1":
                fork = {**row, "head": {"ref": proposal.head, "repo": {"full_name": "fork/app"}}}
                return httpx.Response(200, json=[fork], headers={
                    "Link": '<https://scm.test/?page=2>; rel="next"',
                })
            return httpx.Response(200, json=[row])
        if request.method == "GET" and path == "pulls/7":
            return httpx.Response(200, json={**row, "state": "closed", "merged_at": "2026-01-01"})
        if request.method == "GET" and path == "issues/7/comments":
            return httpx.Response(200, json=comments)

        body = json.loads(request.content)
        if path == "pulls":
            expected = {"head": "sca/1234", "base": "release", "body": proposal.body}
            expected |= {"title": "Fix", "draft": True} if provider is GitHubProvider else {
                "title": "WIP: Fix",
            }
            assert request.method == "POST" and body == expected
            return httpx.Response(201, json=row)
        if path == "pulls/7":
            assert request.method == "PATCH" and body == {"body": "new body"}
            return httpx.Response(200, json=row)
        if path == "issues/7/comments":
            assert request.method == "POST"
            comments.append(body)
            return httpx.Response(201, json=body)
        assert request.method == merge_method and path == "pulls/7/merge" and body == merge_body
        return httpx.Response(200, json={"merged": True}) if provider is GitHubProvider else (
            httpx.Response(200)
        )

    async with httpx.AsyncClient(base_url=repo.api_url, transport=httpx.MockTransport(handler)) as http:
        api = provider(http, repo, token)
        found = await api.find_pull_request(proposal.head, proposal.base, proposal.body)
        assert found is not None and found.ref.number == 7
        assert (await api.open_pull_request(proposal)).ref.state == "open"
        await api.update_pull_request_body(7, "new body")
        for _ in range(2):
            await api.comment(7, "Approved", "<!-- decision -->")
        await api.merge_pull_request(7, "abc")
        assert (await api.get_pull_request(7)).ref.state == "merged"
    assert comments == [{"body": "Approved\n\n<!-- decision -->"}]
    assert calls.count(("GET", "pulls")) == 2


@pytest.mark.parametrize("status", [302, 403, 502])
async def test_provider_errors_are_sanitized_and_redirects_are_refused(status):
    repo = identify_repository(Settings(_env_file=None), "https://github.com/acme/app.git")
    secret = token_urlsafe()
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(status, text=secret, headers={"Location": "https://other.test"})

    async with httpx.AsyncClient(
        base_url=repo.api_url, follow_redirects=True, transport=httpx.MockTransport(handler),
    ) as http:
        with pytest.raises(ScmError) as error:
            await GitHubProvider(http, repo, secret).get_pull_request(1)
    assert error.value.status == status and secret not in str(error.value)
    assert calls == ["api.github.com"]


def test_pull_request_spec_defaults():
    spec = CodingSpec(repo_url="https://github.com/acme/app.git", instruction="Fix", push=True,
                      pull_request={})
    assert spec.pull_request is not None
    assert spec.pull_request.open_on == "review"
    assert not spec.pull_request.draft and not spec.pull_request.merge_on_approve


@pytest.mark.parametrize("overrides", [
    {"push": False}, {"pull_request": {"open_on": "submit"}},
    {"auto_finish": True, "pull_request": {"open_on": "approve"}},
    {"auto_finish": True, "pull_request": {"merge_on_approve": True}},
    {"pull_request": {"draft": True, "typo": True}},
])
def test_invalid_pull_request_specs(overrides):
    values = {"repo_url": "https://github.com/acme/app.git", "instruction": "Fix", "push": True,
              "pull_request": {}}
    with pytest.raises(ValidationError):
        CodingSpec.model_validate(values | overrides)


@pytest.mark.parametrize("remote", [
    "file:///acme/app.git", "https://github.com.evil.test/acme/app.git",
    "https://github.com@evil.test/acme/app.git", "https://github.com/acme/../app.git",
])
def test_unsupported_repository_urls(remote):
    with pytest.raises(ValueError):
        identify_repository(Settings(_env_file=None), remote)
