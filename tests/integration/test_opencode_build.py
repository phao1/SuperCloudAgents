import asyncio
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from sca.agents.opencode_build import OpenCodeBuildWorkflow
from sca.config import get_settings

pytestmark = pytest.mark.integration


async def test_build_workflow_dispatches_record():
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    queue = f"opencode-test-{uuid4()}"
    record = {"id": str(uuid4()), "version": "2.3.0-sca.2", "ref": "fix/unicode"}

    @activity.defn(name="opencode_build")
    async def fake_build(request: dict) -> dict:
        assert request == record
        return {**request, "status": "succeeded", "commit": "a" * 40}

    async with Worker(
        client, task_queue=queue, workflows=[OpenCodeBuildWorkflow], activities=[fake_build]
    ):
        async with asyncio.timeout(20):
            result = await client.execute_workflow(
                OpenCodeBuildWorkflow.run, record, id=f"opencode-test-{uuid4()}", task_queue=queue
            )
    assert result["status"] == "succeeded"
    assert result["commit"] == "a" * 40
