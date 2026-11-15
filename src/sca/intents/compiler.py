"""Worker-side compilation; never executes user tools or fetches remote references."""

from pathlib import Path
from tempfile import TemporaryDirectory

from sca.core.interfaces import ObjectStore
from sca.intents.base import (
    MAX_SOURCE_BYTES,
    MAX_SOURCES,
    IntentBundle,
    IntentSource,
    get,
    register,
)
from sca.intents.openapi import OpenAPIAdapter
from sca.intents.rules import RulesAdapter
from sca.intents.visual import VisualAdapter


def load_builtin() -> None:
    for adapter in (OpenAPIAdapter(), VisualAdapter(), RulesAdapter()):
        try:
            get(adapter.type)
        except ValueError:
            register(adapter)


async def compile_sources(
    sources: list[IntentSource], workspace: Path, objects: ObjectStore, tenant_id: str
) -> IntentBundle:
    if len(sources) > MAX_SOURCES or len({s.name for s in sources}) != len(sources):
        raise ValueError("intent sources must have unique names and at most 16 entries")
    load_builtin()
    result = IntentBundle()
    for source in sources:
        adapter = get(source.type)
        if source.file_id:
            data = await objects.get(f"tenants/{tenant_id}/files/{source.file_id}")
            if len(data) > MAX_SOURCE_BYTES:
                raise ValueError("intent source exceeds size limit")
            with TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "input").write_bytes(data)
                bundle = adapter.compile(source.model_copy(update={"path": "input"}), root)
        else:
            bundle = adapter.compile(source, workspace)
        for name, data in bundle.context_files.items():
            if name in result.context_files and result.context_files[name] != data:
                raise ValueError(f"conflicting context file: {name}")
            if len(data) > MAX_SOURCE_BYTES:
                raise ValueError("context file exceeds size limit")
            result.context_files[name] = data
        result.checks.extend(bundle.checks)
        result.setup.extend(bundle.setup)
        result.prompt_notes.extend(bundle.prompt_notes)
    if len(result.context_files) > 128:
        raise ValueError("too many context files")
    return result


def write_context(workspace: Path, bundle: IntentBundle) -> None:
    # Reject symlinked directories before touching a previously sandbox-owned tree.
    root = workspace
    for part in (".sca", "context"):
        root = root / part
        if root.is_symlink():
            raise ValueError("symlinked context directory")
        root.mkdir(exist_ok=True)
    import os
    import tempfile

    for name, data in bundle.context_files.items():
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid context filename")
        fd, temporary = tempfile.mkstemp(dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, root / name)
        finally:
            Path(temporary).unlink(missing_ok=True)
