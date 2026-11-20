"""Dated desk bulletins and migration from the paper-memo register."""

from dataclasses import dataclass

from .application import Command, Context
from .errors import fail
from .fields import Row, date_field, id_field, optional_text, text_field
from .query import selected
from .records import Entity
from .storage import Store, Table

NAME = "bulletins"
TABLE = Table(NAME, 2)
EVENTS = {"bulletins.posted", "bulletins.retired"}
POLICY = {"bulletins.read": "reader", "bulletins.post": "operator", "bulletins.retire": "manager"}
MESSAGES = {
    "en": {
        "bulletins.list": "Desk bulletins",
        "bulletins.post": "Bulletin posted",
        "bulletins.retire": "Bulletin retired",
    },
    "fr": {
        "bulletins.list": "Avis du bureau",
        "bulletins.post": "Avis affiché",
        "bulletins.retire": "Avis retiré",
    },
}


@dataclass(frozen=True)
class BulletinDesk:
    scope_table: str

    def _validate_memo(self, store: Store, row: Row) -> None:
        store.require(self.scope_table, row["scope"])

    def memo_register(self) -> Entity:
        return Entity(
            "desk-memos",
            "paper memo",
            "note papier",
            (
                id_field("scope", "desk scope"),
                date_field(),
                text_field("message", "note text"),
                text_field("author", "author on the memo"),
            ),
            self._validate_memo,
            ("scope",),
        )

    def post(self, ctx: Context, id: str, scope: str, day: str, expires: str, message: str) -> Row:
        ctx.store.require(self.scope_table, scope)
        if expires < day:
            fail("bad_request", "bulletin expires before publication")
        record = ctx.store.insert(
            NAME,
            {
                "id": id,
                "scope": scope,
                "day": day,
                "expires": expires,
                "message": message,
                "author": ctx.principal.name,
                "status": "posted",
                "retired": None,
            },
        )
        ctx.audit("bulletins.posted", id, scope=scope, day=day)
        return record

    def retire(self, ctx: Context, id: str, day: str) -> Row:
        row = ctx.store.require(NAME, id)
        if row["status"] != "posted" or day < row["day"]:
            fail("conflict", "only a posted bulletin can be retired on or after publication")
        saved = ctx.store.put(NAME, {**row, "status": "retired", "retired": day})
        ctx.audit("bulletins.retired", id, scope=row["scope"], day=day)
        return saved

    def list(self, ctx: Context, scope: str, status: str | None) -> list[Row]:
        ctx.store.require(self.scope_table, scope)
        return selected(ctx.store.rows(NAME), scope=scope, status=status)

    def commands(self) -> tuple[Command, ...]:
        return (
            Command(
                NAME,
                "post",
                "POST",
                "/v1/bulletins/post",
                "bulletins.post",
                "bulletins.post",
                "Post a dated notice",
                (
                    id_field(),
                    id_field("scope", "desk scope"),
                    date_field(),
                    date_field("expires", "last visible day"),
                    text_field("message", "notice text"),
                ),
                self.post,
            ),
            Command(
                NAME,
                "retire",
                "POST",
                "/v1/bulletins/retire",
                "bulletins.retire",
                "bulletins.retire",
                "Retire a notice",
                (id_field(), date_field()),
                self.retire,
            ),
            Command(
                NAME,
                "list",
                "GET",
                "/v1/bulletins/list",
                "bulletins.read",
                "bulletins.list",
                "List notices for a desk",
                (id_field("scope", "desk scope"), optional_text("status", "notice status")),
                self.list,
            ),
        )


def from_paper(store: Store) -> list[Row]:
    return [
        {
            **{k: v for k, v in memo.items() if k != "version"},
            "expires": memo["day"],
            "status": "posted",
            "retired": None,
        }
        for memo in store.rows("desk-memos")
    ]


def visible(ctx: Context, scope: str, day: str) -> list[str]:
    return [
        row["id"]
        for row in selected(ctx.store.rows(NAME), scope=scope)
        if row["day"] <= day <= row["expires"] and (row["retired"] is None or day < row["retired"])
    ]
