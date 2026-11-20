"""Persistent registers and their current record versions."""

from deskcore import bulletins, followups, intake

from .registry import REGISTERS

TABLES = (
    *(entity.table for entity in REGISTERS),
    bulletins.TABLE,
    intake.TABLE,
    followups.TABLE,
)
