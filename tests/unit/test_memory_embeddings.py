import httpx
import pytest

from sca.config import Settings
from sca.gateway.litellm import GatewayError, LiteLLMGateway


def test_memory_settings(monkeypatch):
    monkeypatch.setenv('SCA_MEMORY_MAX_ITEMS', '7')
    monkeypatch.setenv('SCA_MEMORY_EMBEDDING_MODEL', 'embedding')
    settings = Settings(_env_file=None)
    assert settings.memory_retrieval_top_k == 7
    assert settings.memory_embedding_model == 'embedding'
    assert settings.memory_min_per_category == 3
    assert Settings(memory_retrieval_top_k=2).memory_retrieval_top_k == 2
    with pytest.raises(ValueError):
        Settings(memory_min_per_category=-1)


async def test_embedding_gateway():
    gateway = LiteLLMGateway('http://gateway', 'test', '')
    await gateway._http.aclose()

    def response(request):
        assert request.url.path == '/v1/embeddings'
        return httpx.Response(200, json={'data': [
            {'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})

    gateway._http = httpx.AsyncClient(base_url='http://gateway',
                                     transport=httpx.MockTransport(response))
    try:
        assert await gateway.embed(['a', 'b'], 'model') == [[1, 0], [0, 1]]
        with pytest.raises(GatewayError):
            await gateway.embed(['a'], 'model')
    finally:
        await gateway.aclose()


async def test_embedding_failure_is_optional():
    from sca.memory.embedding import embed

    class BrokenGateway:
        async def embed(self, texts: list[str], model: str) -> list[list[float]]:
            raise RuntimeError('provider failure')

    assert await embed('memory', Settings(memory_embedding_model='fake'), BrokenGateway()) is None
