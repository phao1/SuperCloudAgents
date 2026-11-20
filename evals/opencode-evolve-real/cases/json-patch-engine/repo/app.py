class PatchError(ValueError):
    def __init__(self, index: int, code: str):
        super().__init__(code)
        self.index, self.code = index, code


def apply(document, operations):
    raise NotImplementedError("JSON Patch engine")
