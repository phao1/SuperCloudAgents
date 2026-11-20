"""Move complete packs without assigning remote sequence numbers locally."""

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .errors import RelayError
from .journal import Journal
from .storage import replace_bytes
from .types import Envelope
from .wire import encode_pack, iter_frames

READ_SIZE = 32 * 1024


def chunks(stream: BinaryIO) -> Iterator[bytes]:
    return iter(lambda: stream.read(READ_SIZE), b"")


def load(journal: Journal, path: Path) -> list[Envelope]:
    try:
        with path.open("rb") as stream:
            return journal.append_many(row.notice() for row in iter_frames(chunks(stream)))
    except OSError:
        raise RelayError(f"cannot read {path.name}") from None


def dump(journal: Journal, path: Path, after: int | None = None) -> int:
    rows = journal.all() if after is None else journal.read_after(after)
    replace_bytes(path, encode_pack(rows))
    return len(rows)
