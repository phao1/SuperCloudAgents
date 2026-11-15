from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.routes import router
from sca.core.errors import InvalidTaskError
from sca.core.models import Principal
from sca.kernel import kind as registry
from sca.kinds.coding.kind import CodingKind
from sca.kinds.coding.spec import CodingSpec


def test_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    coding = CodingKind()
    registry.register(coding)
    assert registry.get("coding") is coding
    assert registry.all() == [coding]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(coding)
    with pytest.raises(InvalidTaskError, match="unknown task kind"):
        registry.get("missing")
    hooks = replace(coding.hooks)
    hooks.extensions = []
    assert coding.hooks.extensions


async def test_kinds_schema():
    app = FastAPI()
    app.include_router(router)
    # Use the route's actual dependency, preserving the production authorization policy.
    route = next(r for r in router.routes if getattr(r, "path", None) == "/v1/kinds")
    dependency = route.dependant.dependencies[0].call
    app.dependency_overrides[dependency] = lambda: Principal(tenant_id="test", subject="test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/kinds")
    assert response.status_code == 200
    from sca.kinds.analysis.spec import AnalysisSpec

    kinds = {k["name"]: k["schema"] for k in response.json()}
    assert kinds == {
        "coding": CodingSpec.model_json_schema(),
        "analysis": AnalysisSpec.model_json_schema(),
    }


def test_coding_registration(tmp_path):
    from temporalio import activity, workflow

    from sca.agents.fake import FakeBackend
    from sca.config import Settings
    from sca.kernel.kind import KindDeps
    from sca.kernel.workflow import TaskWorkflow
    from sca.storage.local import LocalObjectStore

    kind = CodingKind()
    deps = KindDeps(
        settings=Settings(data_dir=tmp_path),
        store=None,
        sandbox=None,
        agent=FakeBackend(),
        objects=LocalObjectStore(tmp_path / "objects"),
    )
    names = [activity._Definition.must_from_callable(a).name for a in kind.activities(deps)]
    assert len(names) == len(set(names))
    assert {
        kind.hooks.prepare,
        kind.hooks.checkpoint,
        kind.hooks.verify,
        kind.hooks.publish,
        kind.hooks.initial_prompt,
        kind.hooks.fix_prompt,
        kind.hooks.feedback_prompt,
    } <= set(names)
    # A kind registers only its own activities; the kernel's are registered once by the worker.
    assert all(name.startswith("coding.") for name in names), names
    workflows = {workflow._Definition.must_from_class(w).name: w for w in kind.workflows()}
    assert issubclass(workflows[kind.workflow_name], TaskWorkflow)
    assert set(kind.hooks.extensions) <= workflows.keys()


def test_worker_registers_each_activity_once(tmp_path):
    """Two kinds and the kernel together: no activity name twice, kernel hooks delegated."""
    from temporalio import activity

    from sca.agents.fake import FakeBackend
    from sca.config import Settings
    from sca.kernel.activities import KernelActivities
    from sca.kernel.kind import KindDeps
    from sca.kernel.kind import all as all_kinds
    from sca.storage.local import LocalObjectStore

    deps = KindDeps(
        settings=Settings(data_dir=tmp_path),
        store=None,
        sandbox=None,
        agent=FakeBackend(),
        objects=LocalObjectStore(tmp_path / "objects"),
    )
    kernel = KernelActivities(**{f: getattr(deps, f) for f in deps.__dataclass_fields__})
    acts = kernel.kernel_all() + [a for kind in all_kinds() for a in kind.activities(deps)]
    names = [activity._Definition.must_from_callable(a).name for a in acts]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
    assert {k.name for k in all_kinds()} == {"coding", "analysis"}
