class CronError(ValueError):
    def __init__(self, field: int):
        super().__init__(field)
        self.field = field


class Cron:
    def __init__(self, expression: str):
        raise NotImplementedError("UTC cron calendar")

    def matches(self, when):
        raise NotImplementedError

    def between(self, start, stop):
        raise NotImplementedError
