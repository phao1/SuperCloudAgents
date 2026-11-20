from dataclasses import dataclass


@dataclass(frozen=True)
class Dialect:
    delimiter: str
    width: int
    support: int
    newline: str


class CsvError(ValueError):
    def __init__(self, code: str, offset: int):
        super().__init__(code)
        self.code, self.offset = code, offset


def records(text: str, delimiter: str) -> list[list[str]]:
    raise NotImplementedError("CSV records")


def sniff(text: str) -> Dialect | None:
    raise NotImplementedError("CSV sampler")
