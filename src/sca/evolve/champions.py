"""Resolve the platform champion or tenant rollout for a task."""

import hashlib
from uuid import UUID

from sca.db.repo import Store

PLATFORM_TENANT = "eval"


def rollout_bucket(task_id: UUID | str) -> int:
    return int.from_bytes(hashlib.sha256(str(task_id).encode()).digest(), "big") % 100


async def select_champion(
    store: Store, task_id: UUID, tenant_id: str, agent_version: str, model: str,
    use_champion: bool | None,
) -> dict | None:
    if use_champion is False:
        return None
    platform = store.for_tenant(PLATFORM_TENANT)
    if use_champion is True:
        return await platform.get_active_champion(agent_version, model)

    rollout = await store.for_tenant(tenant_id).get_champion_rollout(agent_version, model)
    if rollout is None and tenant_id != PLATFORM_TENANT:
        rollout = await platform.get_champion_rollout(agent_version, model)
    if rollout is not None and rollout_bucket(task_id) < rollout["percent"]:
        return await platform.get_champion(rollout["champion_id"])
    return None
