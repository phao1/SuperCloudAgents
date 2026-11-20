"""Errors that a desk can return to API and terminal callers."""

from typing import NoReturn


class DeskError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


STATUS = {
    "bad_request": 400,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "bad_store": 422,
    "io_error": 500,
}


def fail(code: str, detail: str = "") -> NoReturn:
    raise DeskError(code, detail)
