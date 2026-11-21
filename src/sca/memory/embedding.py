"""Optional embedding calls; failures must not block memory approval."""
import logging
import math
from contextlib import asynccontextmanager

from sca.config import Settings, get_settings
from sca.core.interfaces import ModelGateway
from sca.gateway.health import ProviderHealth
from sca.gateway.litellm import LiteLLMGateway

logger = logging.getLogger(__name__)


@asynccontextmanager
async def embedding_gateway(settings: Settings, gateway: ModelGateway | None = None):
    owned = None
    if gateway is None and settings.model_gateway_url:
        owned = LiteLLMGateway(settings.model_gateway_url, settings.model_gateway_master_key, "",
                               provider_health=ProviderHealth(settings.data_dir))
        gateway = owned
    try:
        yield gateway
    finally:
        if owned:
            await owned.aclose()


async def embed(text: str, settings: Settings | None = None,
                gateway: ModelGateway | None = None) -> list[float] | None:
    settings = settings or get_settings()
    if not settings.memory_embedding_model:
        return None
    try:
        async with embedding_gateway(settings, gateway) as client:
            if client is None:
                return None
            vectors = await client.embed([text], settings.memory_embedding_model)
            vector = vectors[0]
            if not vector or not all(math.isfinite(x) for x in vector) or not any(vector):
                raise ValueError("invalid vector")
            return vector
    except Exception:  # noqa: BLE001 - optional embeddings must never block approval
        logger.warning("Memory embedding unavailable")
        return None


def literal(vector: list[float]) -> str:
    if not vector or not all(math.isfinite(x) for x in vector):
        raise ValueError("invalid vector")
    return "[" + ",".join(str(float(x)) for x in vector) + "]"
