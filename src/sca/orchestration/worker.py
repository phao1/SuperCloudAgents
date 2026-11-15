"""Worker process: runs workflows and activities. `python -m sca.orchestration.worker`."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from sca.config import Settings, get_settings
from sca.core.interfaces import AgentBackend, ModelGateway
from sca.db.engine import make_engine, make_sessionmaker
from sca.db.repo import Store
from sca.kinds.coding.activities import CodingActivities
from sca.kinds.coding.workflow import CodingSubtaskWorkflow, CodingTaskWorkflow
from sca.observability import setup_tracing, tracing_interceptors
from sca.sandbox.docker import DockerSandboxProvider
from sca.storage import make_object_store


def make_agent_backend(settings: Settings) -> AgentBackend:
    if settings.agent_backend == "fake":
        from sca.agents.fake import FakeBackend

        return FakeBackend()
    if settings.agent_backend == "codex":
        from sca.agents.codex import CodexBackend

        return CodexBackend(
            bin_dir=settings.resolved_codex_bin_dir(),
            # With a model gateway, no provider credential is mounted into sandboxes.
            auth_file=None if settings.model_gateway_url else settings.codex_auth_file.expanduser(),
            default_model=settings.default_model,
        )
    raise ValueError(f"unknown agent backend: {settings.agent_backend}")


def make_sandbox_provider(settings: Settings) -> DockerSandboxProvider:
    return DockerSandboxProvider(
        settings.docker_bin,
        egress_allowlist=settings.egress_allowlist(),
        peers=[settings.model_gateway_container] if settings.model_gateway_url else [],
    )


async def make_gateway(settings: Settings, sandbox: DockerSandboxProvider) -> ModelGateway | None:
    """Attach the gateway to the sandbox network and build its client."""
    if not settings.model_gateway_url:
        return None
    from sca.gateway.litellm import LiteLLMGateway

    await sandbox.ensure_egress()
    address = await sandbox.address_of(settings.model_gateway_container)
    return LiteLLMGateway(
        settings.model_gateway_url,
        settings.model_gateway_master_key,
        sandbox_url=f"http://{address}:{settings.model_gateway_port}/v1",
    )


async def build_worker(client: Client, settings: Settings, store: Store | None = None) -> Worker:
    if store is None:
        store = Store(make_sessionmaker(make_engine(settings.database_url)))
    sandbox = make_sandbox_provider(settings)
    activities = CodingActivities(
        settings=settings,
        store=store,
        sandbox=sandbox,
        agent=make_agent_backend(settings),
        objects=make_object_store(settings),
        gateway=await make_gateway(settings, sandbox),
    )
    return Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[CodingTaskWorkflow, CodingSubtaskWorkflow],
        activities=activities.all(),
        max_concurrent_activities=8,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    setup_tracing("sca-worker", settings)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=tracing_interceptors(),
    )
    logging.getLogger("sca.worker").info(
        "worker started: queue=%s backend=%s", settings.task_queue, settings.agent_backend
    )
    await (await build_worker(client, settings)).run()


if __name__ == "__main__":
    asyncio.run(main())
