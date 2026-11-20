"""Worker process: runs workflows and activities. `python -m sca.orchestration.worker`."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta
from pathlib import Path

from prometheus_client import start_http_server
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.config import Settings, get_settings
from sca.core.interfaces import AgentBackend, ModelGateway, SandboxProvider
from sca.db.engine import make_engine, make_sessionmaker
from sca.db.repo import Store
from sca.eval.activities import EvalActivities
from sca.eval.workflow import EvalRunWorkflow
from sca.evolve.activities import EvolutionActivities
from sca.evolve.scheduler import (
    EvolutionSchedulerActivities,
    EvolutionSchedulerWorkflow,
    ensure_evolution_scheduler,
)
from sca.evolve.workflow import EvolutionWorkflow
from sca.kernel.activities import KernelActivities
from sca.kernel.kind import KindDeps
from sca.kernel.kind import all as all_kinds
from sca.maintenance import MaintenanceActivities, MaintenanceWorkflow
from sca.observability import setup_tracing, tracing_interceptors
from sca.sandbox.docker import DockerSandboxProvider
from sca.secrets import make_secret_provider
from sca.storage import make_object_store


def make_agent_backend(settings: Settings) -> AgentBackend:
    if settings.agent_backend == "fake":
        from sca.agents.fake import FakeBackend

        return FakeBackend()
    if settings.agent_backend == "opencode-brain":
        from sca.agents.opencode_brain import OpenCodeBrainBackend

        return OpenCodeBrainBackend(
            settings.opencode_src_cache, settings.opencode_brain_bun,
            settings.opencode_brain_host, settings.default_model,
            settings.opencode_brain_gateway_url, settings.opencode_brain_docker_prefix,
            settings=settings,
        )
    if settings.agent_backend == "opencode":
        from sca.agents.opencode import OpenCodeBackend

        return OpenCodeBackend(settings.opencode_versions_dir,
                               settings.opencode_default_version, settings.default_model)
    if settings.agent_backend == "pi":
        from sca.agents.pi import PiBackend

        return PiBackend(settings.default_model, settings.pi_version, settings.pi_sandbox_image)
    if settings.agent_backend == "codex":
        from sca.agents.codex import CodexBackend

        return CodexBackend(
            # In sync mode nothing is mounted: the binary comes from the sandbox image.
            bin_dir=(
                Path("/opt/codex")
                if settings.state_transport == "sync"
                else settings.resolved_codex_bin_dir()
            ),
            # With a model gateway, no provider credential is mounted into sandboxes.
            auth_file=None if settings.model_gateway_url else settings.codex_auth_file.expanduser(),
            default_model=settings.default_model,
        )
    raise ValueError(f"unknown agent backend: {settings.agent_backend}")


def make_sandbox_provider(settings: Settings) -> SandboxProvider:
    if settings.sandbox_driver == "k8s":
        from sca.sandbox.k8s import K8sSandboxProvider

        if settings.model_gateway_url:
            raise ValueError("the model gateway is only wired up for the docker sandbox driver")
        return K8sSandboxProvider(
            settings.kubectl_bin,
            context=settings.k8s_context,
            namespace=settings.k8s_namespace,
            egress_allowlist=settings.egress_allowlist(),
            node_selector=settings.k8s_node_selector or None,
            network_policy_flavor=settings.k8s_network_policy_flavor,
            blocked_cidrs=settings.k8s_blocked_cidrs,
            pool_size=settings.k8s_pool_size if settings.state_transport == "sync" else 0,
        )
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
    deps = KindDeps(
        settings=settings,
        store=store,
        sandbox=sandbox,
        agent=make_agent_backend(settings),
        objects=make_object_store(settings),
        secrets=make_secret_provider(settings),
        gateway=await make_gateway(settings, sandbox),
        worker_identity=client.identity,
    )
    maintenance = MaintenanceActivities(settings, store, deps.objects, sandbox, deps.gateway)
    if settings.maintenance_schedule:
        await ensure_maintenance_schedule(client, settings)
    if settings.sandbox_driver == "k8s" and settings.k8s_pool_size:
        await maintenance.replenish_sandbox_pool()  # do not wait an hour for a warm pool
    kernel = _kernel_activities(deps)
    backends = {settings.agent_backend: deps.agent}
    for name in ("fake", "codex", "opencode", "opencode-brain", "pi"):
        if name not in backends:
            try:
                backends[name] = make_agent_backend(settings.model_copy(update={"agent_backend": name}))
            except FileNotFoundError:
                pass
    for backend in backends.values():
        if hasattr(backend, "state_transport"):
            backend.state_transport = settings.state_transport
    kernel.backends = backends
    scenario_activities = []
    for kind in all_kinds():
        acts = kind.activities(deps)
        scenario_activities += acts
        owner = next(
            (a.__self__ for a in acts if isinstance(getattr(a, "__self__", None), KernelActivities)),
            None,
        )
        if owner is not None:
            owner.backends = backends
            kernel.scenarios[kind.name] = owner
    from sca.agents.opencode_build import OpenCodeBuildActivities, OpenCodeBuildWorkflow

    builds = OpenCodeBuildActivities(settings)
    evaluation = EvalActivities(settings, store)
    evolution = EvolutionActivities(settings, store, deps.gateway)
    # Evolution productization
    evolution_scheduler = EvolutionSchedulerActivities(settings, store)
    return Worker(
        client,
        identity=deps.worker_identity,
        task_queue=settings.task_queue,
        workflows=[wf for kind in all_kinds() for wf in kind.workflows()] + [MaintenanceWorkflow, EvalRunWorkflow, OpenCodeBuildWorkflow, EvolutionWorkflow, EvolutionSchedulerWorkflow],
        activities=kernel.kernel_all() + scenario_activities + maintenance.all() + evaluation.all() + evolution.all() + [builds.build] + evolution_scheduler.all(),
        max_concurrent_activities=8,
    )


# SCM
def _kernel_activities(deps: KindDeps) -> KernelActivities:
    from sca.kinds.coding.activities import ScmStateActivities

    return ScmStateActivities(**{f: getattr(deps, f) for f in deps.__dataclass_fields__})


async def ensure_maintenance_schedule(client: Client, settings: Settings) -> None:
    try:
        await client.create_schedule(
            "sca-maintenance",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    MaintenanceWorkflow.run, id="sca-maintenance", task_queue=settings.task_queue
                ),
                spec=ScheduleSpec(
                    intervals=[
                        ScheduleIntervalSpec(
                            every=timedelta(seconds=settings.maintenance_interval_s)
                        )
                    ]
                ),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    setup_tracing("sca-worker", settings)
    runtime = None
    if settings.worker_metrics_port is not None:
        runtime = Runtime(
            telemetry=TelemetryConfig(
                metrics=PrometheusConfig(bind_address=f"127.0.0.1:{settings.worker_metrics_port}")
            )
        )
        from sca.kernel.kind import all as all_kinds
        from sca.observability import business_metrics

        business_metrics.prime([kind.name for kind in all_kinds()])
        start_http_server(settings.worker_metrics_port + 1, addr="127.0.0.1")
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=tracing_interceptors(),
        runtime=runtime,
    )
    logging.getLogger("sca.worker").info(
        "worker started: queue=%s backend=%s", settings.task_queue, settings.agent_backend
    )
    from sca.agents.opencode_brain import BrainPool

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    try:
        await ensure_evolution_scheduler(client, settings)
        await (await build_worker(client, settings)).run()
    finally:
        try:
            await BrainPool.close_all()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    asyncio.run(main())
