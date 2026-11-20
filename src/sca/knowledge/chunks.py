import re
from collections.abc import Iterator
from dataclasses import dataclass

_HEADING = re.compile(r" {0,3}#{1,6}[ \t]+(.+?)\s*#*\s*$")
_PARAGRAPHS = re.compile(r"\n(?:[ \t]*\n)+")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str | None
    content: str


def _sections(text: str) -> Iterator[tuple[str | None, str]]:
    heading = None
    lines: list[str] = []
    for line in text.splitlines():
        if match := _HEADING.fullmatch(line):
            yield heading, "\n".join(lines)
            heading = match[1][:160]
            lines = []
        lines.append(line)
    yield heading, "\n".join(lines)


def _pieces(paragraph: str, limit: int) -> Iterator[str]:
    while len(paragraph) > limit:
        cut = max(paragraph.rfind("\n", 0, limit + 1), paragraph.rfind(" ", 0, limit + 1))
        cut = cut if cut >= limit // 2 and cut > 0 else limit
        yield paragraph[:cut].rstrip()
        paragraph = paragraph[cut:].lstrip()
    if paragraph:
        yield paragraph


def split_document(text: str, limit: int = 1200) -> list[Chunk]:
    if limit < 1:
        raise ValueError("Chunk size must be positive")
    chunks: list[Chunk] = []
    for heading, section in _sections(text):
        current = ""
        for paragraph in _PARAGRAPHS.split(section.strip()):
            for piece in _pieces(paragraph.strip(), limit):
                if current and len(current) + 2 + len(piece) > limit:
                    chunks.append(Chunk(len(chunks) + 1, heading, current))
                    current = ""
                current = f"{current}\n\n{piece}" if current else piece
        if current:
            chunks.append(Chunk(len(chunks) + 1, heading, current))
    return chunks
