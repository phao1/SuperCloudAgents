import httpx
import pytest

from sca.config import get_settings
from sca.gateway.litellm import LiteLLMGateway
from sca.memory.distill import distill
from sca.memory.models import MemorySettings

pytestmark = pytest.mark.gateway


async def test_real_distillation_samples():
    settings = get_settings()
    if not settings.model_gateway_url:
        pytest.skip("model gateway not configured")
    async with httpx.AsyncClient(timeout=5) as http:
        try:
            response = await http.get(settings.model_gateway_url.rstrip("/") + "/health/liveliness")
            response.raise_for_status()
        except httpx.HTTPError:
            pytest.skip("model gateway unreachable")
    gateway = LiteLLMGateway(
        settings.model_gateway_url, settings.model_gateway_master_key, "", timeout_s=90
    )
    try:
        for feedback, diff in [
            ("This repository uses pnpm, never npm.", "-npm install\n+pnpm install"),
            ("Tests must run serially here using --runInBand.", "-jest\n+jest --runInBand"),
            ("Rename helper foo to bar in this patch.", "-def foo():\n+def bar():"),
        ]:
            proposals = await distill(gateway, MemorySettings().memory_model, feedback, diff)
            assert len(proposals) <= 3
            print(proposals)
    finally:
        await gateway._http.aclose()
