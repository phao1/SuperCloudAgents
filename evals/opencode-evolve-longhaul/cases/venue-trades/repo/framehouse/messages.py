"""English and French operator responses."""

from deskcore import bulletins, followups, intake
from deskcore.records import entity_messages

from . import operations
from .registry import REGISTERS

MESSAGES = {
    "en": {
        **entity_messages(REGISTERS, "en"),
        **bulletins.MESSAGES["en"],
        **intake.MESSAGES["en"],
        **followups.MESSAGES["en"],
        **operations.EN,
        "desk.report": "Desk overview",
        "desk.export": "Desk register exported",
    },
    "fr": {
        **entity_messages(REGISTERS, "fr"),
        **bulletins.MESSAGES["fr"],
        **intake.MESSAGES["fr"],
        **followups.MESSAGES["fr"],
        **operations.FR,
        "desk.report": "Vue du bureau",
        "desk.export": "Registre exporté",
    },
}
