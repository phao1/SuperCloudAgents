"""Harbor pack encoder and frame reader."""

from collections.abc import Iterable, Iterator
from struct import Struct
from zlib import crc32

from .encoding import decode, envelope, row_bytes
from .errors import ArchiveError, RelayError
from .types import Envelope

MAGIC = b"HR1\n"
MAX_BODY = 65536
WORD = Struct(">I")


def encode_pack(rows: Iterable[Envelope]) -> bytes:
    pack = bytearray(MAGIC)
    for row in rows:
        body = row_bytes(row)
        if not 1 <= len(body) <= MAX_BODY:
            raise ArchiveError("invalid archive length")
        pack += WORD.pack(len(body))
        pack += body
        pack += WORD.pack(crc32(body))
    return bytes(pack)


def record(body: bytes, checksum: int) -> Envelope:
    if crc32(body) != checksum:
        raise ArchiveError("archive checksum mismatch")
    try:
        return envelope(decode(body))
    except RelayError:
        raise ArchiveError("invalid archive record") from None


def iter_frames(chunks: Iterable[bytes]) -> Iterator[Envelope]:
    raw = b"".join(chunks)
    if not raw.startswith(MAGIC):
        raise ArchiveError("invalid archive header")
    at = len(MAGIC)
    while at < len(raw):
        if len(raw) - at < WORD.size:
            raise ArchiveError("truncated archive frame")
        size, = WORD.unpack_from(raw, at)
        if not 1 <= size <= MAX_BODY:
            raise ArchiveError("invalid archive length")
        at += WORD.size
        end = at + size + WORD.size
        if end > len(raw):
            raise ArchiveError("truncated archive frame")
        yield record(raw[at:at + size], WORD.unpack_from(raw, at + size)[0])
        at = end
