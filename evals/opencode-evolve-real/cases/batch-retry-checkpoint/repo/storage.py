from checkpoint import dump, load


class Storage:
    def __init__(self, checkpoint: str | None = None):
        self.text = checkpoint if checkpoint is not None else dump({})

    def applied(self) -> dict[tuple[str, str], int]:
        return load(self.text)

    def write(self, state: dict[tuple[str, str], int]) -> None:
        self.text = dump(state)
