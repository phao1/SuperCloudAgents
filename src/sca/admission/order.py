"""Pure scheduling order shared by the Postgres admission implementation."""

from fractions import Fraction

PRIORITIES = {"interactive": 0, "eval": 1, "batch": 2}


def grant_order(candidate: dict, held: dict[str, int], weights: dict[str, int]) -> tuple:
    tenant = candidate["tenant_id"]
    return (PRIORITIES[candidate["spec"].get("priority", "interactive")],
            Fraction(held.get(tenant, 0), weights[tenant]),
            candidate["slot_requested_at"], str(candidate["id"]))
