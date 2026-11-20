"""Minimum roles for desk commands."""

from deskcore import bulletins, followups, intake, system

POLICY = {
    **system.POLICY,
    **bulletins.POLICY,
    **intake.POLICY,
    **followups.POLICY,
}
