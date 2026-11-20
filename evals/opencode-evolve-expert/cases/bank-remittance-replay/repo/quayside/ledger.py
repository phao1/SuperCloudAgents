"""Every write publishes a validated replacement snapshot."""

from collections.abc import Callable
from pathlib import Path

from . import storage
from .model import State


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.state = storage.load(path)

    def change[T](self, apply: Callable[..., tuple[State, T]], *args, **kwargs) -> T:
        new, output = apply(self.state, *args, **kwargs)
        if new != self.state:
            storage.write(self.path, new)
            self.state = new
        return output
