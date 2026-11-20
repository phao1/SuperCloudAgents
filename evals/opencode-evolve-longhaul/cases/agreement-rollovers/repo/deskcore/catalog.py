"""Message keys are stable across different desk locales."""

from collections.abc import Mapping

from .errors import DeskError

CORE = {
    "en": {
        "error.bad_request": "Invalid request",
        "error.forbidden": "Permission denied",
        "error.not_found": "Record or command not found",
        "error.conflict": "Operation conflicts with the current desk state",
        "error.bad_store": "Workspace data is invalid",
        "error.io_error": "Workspace I/O failed",
        "store.describe": "Workspace details",
        "store.upgrade": "History upgrade checked",
        "team.whoami": "Current team member",
        "team.list": "Desk team",
        "team.add": "Team member registered",
        "team.suspend": "Team member suspended",
        "audit.list": "Audit trail",
        "audit.export": "Audit trail exported",
        "bundle.export": "Legacy bundle prepared",
        "bundle.import": "Legacy bundle imported",
    },
    "fr": {
        "error.bad_request": "Requête invalide",
        "error.forbidden": "Accès refusé",
        "error.not_found": "Enregistrement ou commande introuvable",
        "error.conflict": "Conflit avec l’état actuel",
        "error.bad_store": "Données du dossier invalides",
        "error.io_error": "Échec de l’écriture du dossier",
        "store.describe": "Détails du dossier",
        "store.upgrade": "Conversion de l’historique vérifiée",
        "team.whoami": "Membre actuel",
        "team.list": "Équipe du bureau",
        "team.add": "Membre inscrit",
        "team.suspend": "Membre suspendu",
        "audit.list": "Journal de suivi",
        "audit.export": "Journal de suivi exporté",
        "bundle.export": "Archive historique préparée",
        "bundle.import": "Archive historique importée",
    },
}


def catalogs(extra: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    return {locale: {**base, **extra.get(locale, {})} for locale, base in CORE.items()}


def message(messages: Mapping[str, Mapping[str, str]], locale: str, key: str) -> str:
    language = messages.get(locale, messages["en"])
    return language.get(key, messages["en"].get(key, key))


def error_message(error: DeskError, messages: Mapping[str, Mapping[str, str]], locale: str) -> str:
    return message(messages, locale, "error." + error.code)
