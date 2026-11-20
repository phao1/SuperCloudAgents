class IniError(ValueError):
    def __init__(self, code: str, line: int, column: int):
        super().__init__(code)
        self.code, self.line, self.column = code, line, column


def parse(text: str) -> dict:
    raise NotImplementedError("INI sources")
