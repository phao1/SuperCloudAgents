from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True)
class Entry:
    path: str
    directory: bool = False


def included(entries: list[Entry], rules: list[str]) -> list[str]:
    result = []
    for entry in entries:
        keep = True
        for rule in rules:
            if not rule or rule.startswith("#"):
                continue
            negate = rule.startswith("!")
            pattern = rule.lstrip("!/").rstrip("/")
            if fnmatch(entry.path, pattern) or fnmatch(entry.path.rsplit("/", 1)[-1], pattern):
                keep = negate
        if keep:
            result.append(entry.path)
    return result
