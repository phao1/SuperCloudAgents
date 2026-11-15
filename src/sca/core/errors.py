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
