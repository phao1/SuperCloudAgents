class FrameError(ValueError):
    def __init__(self, code: str, offset: int):
        super().__init__(code)
        self.code, self.offset = code, offset


def encode(kind: int, payload: bytes) -> bytes:
    raise NotImplementedError("record encoder")


class Decoder:
    def feed(self, chunk: bytes) -> list[tuple[int, bytes]]:
        raise NotImplementedError("record decoder")

    def finish(self) -> None:
        raise NotImplementedError("record decoder")
