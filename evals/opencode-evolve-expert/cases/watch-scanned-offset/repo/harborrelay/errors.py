class RelayError(ValueError):
    """Recoverable invalid input or unreadable desk state."""


class ArchiveError(RelayError):
    """A pack does not satisfy the wire format."""
