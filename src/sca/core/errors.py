"""Error types shared across the platform."""


class ScaError(Exception):
    """Base class for platform errors."""


class NotFoundError(ScaError):
    pass


class ConflictError(ScaError):
    """The request is valid but not allowed in the current state."""


class AuthError(ScaError):
    pass


class InvalidTaskError(ScaError):
    """The task can never succeed as specified (bad repo URL, bad ref, bad spec).

    Activities raise this to tell the workflow not to retry.
    """


class SandboxError(ScaError):
    pass


class AgentTurnError(ScaError):
    """An agent turn ended abnormally (non-zero exit, timeout, or no completion event)."""


class WorkspaceError(ScaError):
    pass


# Model gateway budget refusals
GATEWAY_BUDGET_REFUSAL = "model gateway budget refused"
MODEL_BUDGET_EXHAUSTED = "预算耗尽"
MODEL_BUDGET_MISCONFIGURED = "预算配置错误，网关拒绝了首个模型请求。请检查预算和 DSH 输出 Token 上限"
MODEL_BUDGET_REASONS = (MODEL_BUDGET_EXHAUSTED, MODEL_BUDGET_MISCONFIGURED)


class ModelBudgetRefused(AgentTurnError):
    """The gateway denied a model request under its budget."""

    def __init__(self, *, model_responded: bool) -> None:
        self.model_responded = model_responded
        super().__init__(GATEWAY_BUDGET_REFUSAL)
