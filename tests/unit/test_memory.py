import json

import httpx
import pytest

from sca.gateway.litellm import LiteLLMGateway
from sca.memory.context import write_memory
from sca.memory.distill import distill, parse_candidates, parse_markdown
from sca.memory.models import MemoryInput, normalise_scope, render


def test_limits_and_normalisation():
    assert normalise_scope("repo:HTTPS://Example.COM/a/#fragment") == "repo:https://example.com/a"
    for data in (
        {"scope": "invalid", "text": "test"},
        {"scope": "tenant", "text": " " * 3},
        {"scope": "tenant", "text": "x" * 501},
    ):
        with pytest.raises(ValueError):
            MemoryInput.model_validate(data)
    rows = [{"id": str(i), "text": "x" * 100, "category": "convention"} for i in range(40)]
    content, ids = render(rows)
    assert len(content) <= 3000 and len(ids) <= 30
    assert len(render(rows, 2)[1]) == 2


def test_untrusted_proposals_and_context(tmp_path):
    parsed = parse_markdown("# Memory\n- [command] pnpm test\n* convention\n- " + "x" * 501)
    assert len(parsed) == 2 and parsed[0]["category"] == "command"
    for value in (
        "not json",
        '[{"text":"x","category":"invalid"}]',
        json.dumps([{"text": "x"}] * 4),
        "x" * 10001,
    ):
        with pytest.raises(ValueError):
            parse_candidates(value)
    write_memory(tmp_path, "trusted platform write")
    external = tmp_path / "external"
    external.mkdir()
    target = tmp_path / ".sca/context/MEMORY.md"
    target.unlink()
    target.symlink_to(external / "secret")
    write_memory(tmp_path, "replacement")
    assert not (external / "secret").exists()
    assert target.read_text() == "replacement"
    target.unlink()
    target.parent.rmdir()
    target.parent.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        write_memory(tmp_path, "unsafe")


async def test_gateway_completion_and_distillation():
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '[{"text":"Use pnpm","category":"command"}]',
                        }
                    }
                ]
            },
        )

    gateway = LiteLLMGateway("http://gateway", "private-key", "http://sandbox/v1")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(respond)
    )
    try:
        assert await distill(gateway, "cheap", "Use pnpm, please", "diff") == [
            {"text": "Use pnpm", "category": "command"}
        ]
        assert seen[0]["model"] == "cheap" and len(seen[0]["messages"]) == 2
        assert "private-key" not in str(seen)
    finally:
        await gateway._http.aclose()
