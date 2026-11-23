"""Deterministic evaluation failures shared by activities and workflows."""

from temporalio.exceptions import ActivityError, ApplicationError

SUITE_CHANGED = "SuiteChanged"
INVALID_EVAL_INPUT = "InvalidEvalInput"
GUIDANCE_ABORT = "已因指引中止"


def suite_changed(message: str) -> ApplicationError:
    return ApplicationError(message, type=SUITE_CHANGED, non_retryable=True)


def invalid_eval_input(message: str) -> ApplicationError:
    return ApplicationError(message, type=INVALID_EVAL_INPUT, non_retryable=True)


def eval_failure_reason(error: ActivityError) -> str | None:
    cause = error.cause
    if (isinstance(cause, ApplicationError) and cause.non_retryable
            and cause.type in (SUITE_CHANGED, INVALID_EVAL_INPUT)):
        return cause.message
    return None
