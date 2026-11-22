"""Normalize model errors at the CLI boundary before recording agent events."""

from sca.core.errors import GATEWAY_BUDGET_REFUSAL
from sca.core.model_provider import classify_model_error, gateway_budget_refused
from sca.core.models import AgentEvent


def model_error_event(error: object, fallback: str) -> AgentEvent:
    provider = classify_model_error(error)
    if provider is not None:
        return AgentEvent(type="error", data={"message": str(provider),
                                              "model_provider_error": provider.payload()})
    if gateway_budget_refused(error):
        fallback = GATEWAY_BUDGET_REFUSAL
    return AgentEvent(type="error", data={"message": fallback})
