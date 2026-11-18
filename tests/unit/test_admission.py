from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sca.admission.order import grant_order
from sca.admission.pressure import gateway_pressure
from sca.config import get_settings


def candidate(tenant, priority="interactive", age=0):
    return {"id": uuid4(), "tenant_id": tenant, "spec": {"priority": priority},
            "slot_requested_at": datetime.now(UTC) - timedelta(seconds=age)}


def test_priority_weight_then_age():
    held, weights = {"a": 2, "b": 1}, {"a": 4, "b": 1}
    rows = [candidate("a", "batch", 100), candidate("b", age=100),
            candidate("a"), candidate("a", age=10), candidate("a", "eval", 100)]
    assert sorted(rows, key=lambda r: grant_order(r, held, weights)) == [
        rows[3], rows[2], rows[1], rows[4], rows[0]]


@pytest.mark.parametrize("content,expected", [
    ('{"saturated": true, "updated_at": "2000-01-01T00:00:00+00:00"}', False),
    ('invalid', False), ('[]', False),
])
async def test_pressure_invalid_and_stale(tmp_path, monkeypatch, content, expected):
    path = tmp_path / "pressure.json"
    path.write_text(content)
    monkeypatch.setattr(get_settings(), "gateway_pressure_file", path)
    assert await gateway_pressure() is expected


async def test_pressure_recent(tmp_path, monkeypatch):
    import json

    path = tmp_path / "pressure.json"
    path.write_text(json.dumps({"saturated": True, "updated_at": datetime.now(UTC).isoformat()}))
    monkeypatch.setattr(get_settings(), "gateway_pressure_file", path)
    assert await gateway_pressure()
