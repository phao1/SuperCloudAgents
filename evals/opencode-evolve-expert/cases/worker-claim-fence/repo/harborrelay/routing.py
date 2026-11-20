"""Preview the worklists interested in a stream."""

from collections.abc import Iterable

from .directory import Directory
from .matching import RouteMatcher
from .types import Route
from .validation import parts


class RoutingTable:
    def __init__(self, rules: Iterable[Route]):
        self.routes = [(rule.name, RouteMatcher(rule)) for rule in rules]

    def targets(self, stream: str) -> list[str]:
        address = parts(stream)
        return [name for name, filter_ in self.routes if filter_.accepts(address)]


def preview(directory: Directory, stream: str) -> list[str]:
    return RoutingTable(directory.list()).targets(stream)
