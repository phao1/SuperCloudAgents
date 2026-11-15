from pathlib import Path

from sca.intents.base import IntentBundle
from sca.intents.compiler import write_context


def write_memory(workspace: Path, content: str) -> None:
    write_context(workspace, IntentBundle(context_files={"MEMORY.md": content.encode()}))
