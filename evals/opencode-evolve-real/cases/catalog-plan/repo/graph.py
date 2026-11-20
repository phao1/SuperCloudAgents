from dataclasses import dataclass


@dataclass(frozen=True)
class Package:
    """A version and an ordered list of dependencies.

    A dependency prefix "?" makes that dependency optional. The prefix
    is not part of the name and appears only in dependency lists, never
    in targets or aliases. Optional only suppresses an absent terminal
    name, not cycles or problems in packages that are present.
    """

    version: str
    requires: list[str]


def traverse(targets: list[str], records: dict[str, Package], aliases: dict[str, str]):
    ordered, seen = [], set()
    def visit(name, parent):
        if name in seen:
            return
        seen.add(name)
        for dependency in records[name].requires:
            visit(dependency, name)
        ordered.append((name, records[name].version, parent))

    for name in targets:
        visit(name, None)
    return ordered, []
