"""The account failure payload shared by Temporal activities and workflows."""

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from sca.core.model_provider import ModelProviderError

_ERROR_TYPE = "ModelProviderError"


def account_failure(error: ModelProviderError) -> ApplicationError:
    return ApplicationError(error.reason, error.payload(), type=_ERROR_TYPE, non_retryable=True)


def provider_account_error(error: BaseException | None) -> ModelProviderError | None:
    while isinstance(error, (ActivityError, ChildWorkflowError)):
        error = error.cause
    if (isinstance(error, ApplicationError) and error.type == _ERROR_TYPE
            and error.non_retryable and error.details):
        provider = ModelProviderError.from_payload(error.details[0])
        if provider is not None and provider.is_account_error:
            return provider
    return None


def patched_provider_account_error(error: BaseException) -> ModelProviderError | None:
    provider = provider_account_error(error)
    return provider if provider is not None and workflow.patched("model-provider-errors") else None
