"""Segment matching for desk route rules."""

from .types import Route
from .validation import parts


def matches(pattern: tuple[str, ...], stream: tuple[str, ...]) -> bool:
    return len(pattern) == len(stream) and all(
        test == "*" or test == actual for test, actual in zip(pattern, stream, strict=True)
    )


class RouteMatcher:
    def __init__(self, route: Route):
        self.include = tuple(parts(p, field="pattern", wild=True) for p in route.include)
        self.exclude = tuple(parts(p, field="pattern", wild=True) for p in route.exclude)

    def accepts(self, stream: tuple[str, ...]) -> bool:
        return any(matches(rule, stream) for rule in self.include) and not any(
            matches(rule, stream) for rule in self.exclude
        )
