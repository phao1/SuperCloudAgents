"""The desk is the write boundary for the local service."""

from collections.abc import Callable
from pathlib import Path

from . import storage
from .model import State


class Desk:
    def __init__(self, path: Path):
        self.path = path
        self.state = storage.load(path)

    def change[T](self, operation: Callable[..., tuple[State, T]], *args, **kwargs) -> T:
        updated, value = operation(self.state, *args, **kwargs)
        if updated != self.state:
            storage.save(self.path, updated)
            self.state = updated
        return value
