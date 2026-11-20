"""Assemble one application instance for both surfaces."""

from deskcore.application import Application

from .events import EVENTS
from .messages import MESSAGES
from .migrations import UPGRADES
from .policy import POLICY
from .registry import COMMANDS
from .schema import TABLES

APP = Application(COMMANDS, TABLES, POLICY, MESSAGES, EVENTS, UPGRADES)
