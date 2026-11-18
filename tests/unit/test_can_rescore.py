from sca.api.evals import can_rescore
from sca.core.models import Principal


def principal(**fields) -> Principal:
    return Principal(**{"subject": "u", "tenant_id": "eval", **fields})


def test_can_rescore_follows_rescore_permission():
    assert can_rescore(principal(scopes=["eval:admin"]))
    assert not can_rescore(principal(scopes=["read"]))
    assert can_rescore(principal(role="admin"))
    assert not can_rescore(principal(role="viewer"))
    assert not can_rescore(principal(tenant_id="default", scopes=["eval:admin"]))
