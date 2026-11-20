class TemplateError(ValueError):
    def __init__(self, code: str, line: int, column: int):
        super().__init__(code)
        self.code, self.line, self.column = code, line, column


def render(template: str, data: dict) -> str:
    raise NotImplementedError("Pocket templates")
