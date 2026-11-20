"""Audit events emitted by this application."""

from deskcore import bulletins, followups, intake, system
from deskcore.records import entity_events

from . import operations
from .registry import REGISTERS

EVENTS = {
    *system.EVENTS,
    *entity_events(REGISTERS),
    *operations.EVENTS,
    *bulletins.EVENTS,
    *intake.EVENTS,
    *followups.EVENTS,
}
