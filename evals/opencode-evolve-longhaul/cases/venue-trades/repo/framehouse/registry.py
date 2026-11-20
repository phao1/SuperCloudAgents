"""Shared registration for the CLI and in-process API."""

from deskcore import bulletins, followups, intake, system
from deskcore.overview import Overview
from deskcore.records import entity_commands

from . import operations, reporting
from .records import ENTITIES

SCOPE_TABLE = "venues"
BULLETINS = bulletins.BulletinDesk(SCOPE_TABLE)
REGISTERS = (*ENTITIES, BULLETINS.memo_register())
INTAKE = intake.Intake(REGISTERS)
FOLLOWUPS = followups.Followups(SCOPE_TABLE, REGISTERS)
OVERVIEW = Overview(SCOPE_TABLE, REGISTERS, reporting.sections)

COMMANDS = (
    *system.commands(REGISTERS),
    *entity_commands(REGISTERS),
    *operations.COMMANDS,
    *OVERVIEW.commands(),
    *BULLETINS.commands(),
    *INTAKE.commands(),
    *FOLLOWUPS.commands(),
)
