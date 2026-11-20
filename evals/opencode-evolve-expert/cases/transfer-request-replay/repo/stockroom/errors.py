"""Expected errors that may be presented at the command line."""


class StockroomError(ValueError):
    pass


class Missing(StockroomError):
    pass


class Conflict(StockroomError):
    pass
