"""Atomic local file replacement for desk state and export packs."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .errors import RelayError


def read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise RelayError(f"cannot read {path.name}") from None


def replace_bytes(path: Path, content: bytes) -> None:
    name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as out:
            name = out.name
            out.write(content)
        os.replace(name, path)
        name = None
    except OSError:
        raise RelayError(f"cannot write {path.name}") from None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)
