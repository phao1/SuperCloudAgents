"""Sandbox entry point for design token literals in changed styles."""

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
UNITS = r"rem|em|px|vh|vw|vmin|vmax|ch|ex|cm|mm|in|pt|pc|%"
LENGTH = rf"{NUMBER}(?:{UNITS})"
HEX = r"\#(?:[\da-f]{8}|[\da-f]{6}|[\da-f]{4}|[\da-f]{3})(?![\w-])"
COLOR_FUNCTION = r"(?:rgba?|hsla?|hwb|lch|oklch|lab|oklab|color)\([^()]+\)"
_LITERAL = re.compile(rf"(?<![\w.-])(?:{HEX}|{COLOR_FUNCTION}|{LENGTH}(?![\w%]))", re.IGNORECASE)
_WORDS = re.compile(r"(?<![\w-])[a-zA-Z][\w-]*")
_IGNORE = re.compile(r"/\*[\s\S]*?\*/|(?m:^[ \t]*//[^\n]*)|(?:var|url)\([^()]*\)", re.IGNORECASE)
_PROP = r"(?P<prop>[a-zA-Z-][\w-]*)"
_QUOTED = r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
_DECL = re.compile(
    rf"(?:^|(?<=[{{;,\n]))[ \t]*{_PROP}[ \t]*:[ \t]*"
    rf"(?P<value>{_QUOTED}|(?:\([^()]*\)|[^;{{}},\x60()<])+)"
)
_JSX_ATTR = re.compile(rf"\b{_PROP}\s*=\s*(?P<value>{_QUOTED}|\{{\s*(?:{NUMBER}|{_QUOTED})\s*\}})")
_COLOR_PROP = re.compile(r"color|background|border|shadow|fill|stroke|outline|decoration")
_DIMENSION_PROP = re.compile(
    r"(?:^(?:top|right|bottom|left|width|height|gap|flex-basis|font-size|letter-spacing|"
    r"text-indent|perspective|translate|x|y|cx|cy|r|rx|ry)$|"
    r"(?:width|height|radius|gap|^padding|^margin|^inset))"
)


def _rgb8(amount: str, maximum: int = 255) -> int:
    value = (
        Decimal(amount[:-1]) * 255 / 100
        if amount.endswith("%")
        else Decimal(amount) * 255 / maximum
    )
    return round(max(Decimal(0), min(Decimal(255), value)))


def literal_key(value: str) -> str:
    """Canonicalize equivalent hex/rgb colors and decimal spellings of lengths."""
    value = value.strip().lower()
    dimension = re.fullmatch(rf"({NUMBER})({UNITS})", value)
    if dimension:
        return format(Decimal(dimension[1]).normalize(), "f") + dimension[2]
    if re.fullmatch(HEX, value):
        digits = value[1:]
        if len(digits) in {3, 4}:
            digits = "".join(char * 2 for char in digits)
        return "#" + (digits[:-2] if len(digits) == 8 and digits.endswith("ff") else digits)
    rgb = re.fullmatch(r"rgba?\(([^()]+)\)", value)
    srgb = re.fullmatch(r"color\(srgb\s+([^()]+)\)", value)
    if match := rgb or srgb:
        parts = re.split(r"[\s,/]+", match[1].strip())
        if len(parts) in {3, 4}:
            try:
                digits = "".join(f"{_rgb8(part, 1 if srgb else 255):02x}" for part in parts[:3])
                alpha = _rgb8(parts[3], 1) if len(parts) == 4 else 255
                return "#" + digits + (f"{alpha:02x}" if alpha < 255 else "")
            except (InvalidOperation, ValueError):
                pass
    return re.sub(r"\s*([(),/])\s*", r"\1", " ".join(value.split()))


def _masked(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match[0])


def violations(path: str, text: str, literals: dict[str, list[str]]) -> Iterator[str]:
    tsx = path.endswith(".tsx")
    clean = _IGNORE.sub(_masked, text)
    matches = [*_DECL.finditer(clean), *(_JSX_ATTR.finditer(clean) if tsx else ())]
    seen = set()
    for match in sorted(matches, key=lambda item: item.start("value")):
        prop = re.sub(r"([A-Z])", r"-\1", match["prop"]).lower()
        if prop.startswith(("--", "aria-", "data-")) or prop in {"content", "font-family"}:
            continue
        raw = match["value"]
        value = raw.strip()
        if tsx:
            relevant = _COLOR_PROP.search(prop) or _DIMENSION_PROP.search(prop)
            if match.re is _JSX_ATTR and not relevant:
                continue
            value = value.strip("{}").strip().strip("\"'")
        elif value.startswith(("'", '"')):
            continue
        if not value:
            continue
        offset = match.start("value") + raw.find(value)
        line = text.count("\n", 0, offset) + 1
        found = [item[0] for item in _LITERAL.finditer(value)]
        if _COLOR_PROP.search(prop):
            remainder = _LITERAL.sub(_masked, value)
            found.extend(item[0] for item in _WORDS.finditer(remainder))
        if (
            tsx
            and prop != "line-height"
            and _DIMENSION_PROP.search(prop)
            and re.fullmatch(NUMBER, value)
        ):
            found.append(value + "px")
        for candidate in found:
            key = literal_key(candidate)
            if key in literals and (line, key) not in seen:
                seen.add((line, key))
                yield f"{path}:{line}: hard-coded {candidate}. Use {', '.join(literals[key])}"


def _git(*args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", *args], stderr=subprocess.PIPE
    )


def main(config: Path, base: str) -> int:
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base):
        print("Design token check requires a Git base commit.")
        return 2
    literals = json.loads(config.read_bytes())
    changed = _git("diff", "--name-only", "-z", "--diff-filter=ACMRT", base, "HEAD", "--")
    failed = False
    for raw_path in changed.split(b"\0"):
        path = raw_path.decode("utf-8", "surrogateescape")
        if path.startswith(".sca/") or PurePosixPath(path).suffix not in {".css", ".scss", ".tsx"}:
            continue
        content = _git("cat-file", "blob", f"HEAD:{path}").decode("utf-8", "replace")
        for message in violations(path, content, literals):
            print(message)
            failed = True
    return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main(Path(sys.argv[1]), sys.argv[2]))
    except subprocess.CalledProcessError:
        sys.exit("Design token check could not read the Git changes.")
