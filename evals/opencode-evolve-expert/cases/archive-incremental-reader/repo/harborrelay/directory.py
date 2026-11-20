"""The named subscriber routes persisted independently of incoming notices."""

from dataclasses import asdict
from pathlib import Path

from .encoding import as_bytes, decode
from .errors import RelayError
from .storage import read_optional, replace_bytes
from .types import Route
from .validation import label, parts


def route(name: str, include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> Route:
    label(name, "route")
    if not include:
        raise RelayError("route needs an inclusion")
    for value in (*include, *exclude):
        parts(value, field="pattern", wild=True)
    return Route(name, tuple(dict.fromkeys(include)), tuple(dict.fromkeys(exclude)))


class Directory:
    def __init__(self, home: Path):
        self.path = home / "routes.json"
        self._routes = self._load()

    def _load(self) -> dict[str, Route]:
        raw = read_optional(self.path)
        if raw is None:
            return {}
        try:
            records = decode(raw)
            if not isinstance(records, list):
                raise RelayError("invalid route directory")
            result = {}
            for row in records:
                if (not isinstance(row, dict) or row.keys() != {"name", "include", "exclude"}
                        or not isinstance(row["include"], list)
                        or not isinstance(row["exclude"], list)):
                    raise RelayError("invalid route row")
                entry = route(row["name"], tuple(row["include"]), tuple(row["exclude"]))
                if entry.name in result:
                    raise RelayError("duplicate route")
                result[entry.name] = entry
            return result
        except RelayError:
            raise RelayError("invalid route directory") from None

    def get(self, name: str) -> Route:
        label(name, "route")
        try:
            return self._routes[name]
        except KeyError:
            raise RelayError("unknown route") from None

    def list(self) -> tuple[Route, ...]:
        return tuple(self._routes.values())

    def _write(self, values: dict[str, Route]) -> None:
        replace_bytes(self.path, as_bytes([asdict(row) for row in values.values()]) + b"\n")
        self._routes = values

    def register(self, name: str, include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> Route:
        value = route(name, include, exclude)
        self._write({**self._routes, name: value})
        return value

    def drop(self, name: str) -> Route:
        previous = self.get(name)
        updated = self._routes.copy()
        del updated[name]
        self._write(updated)
        return previous
