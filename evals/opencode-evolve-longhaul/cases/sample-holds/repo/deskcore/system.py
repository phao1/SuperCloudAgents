"""Common command groups used by local desks."""

from collections.abc import Sequence
from functools import partial

from . import audit, exchange, history, team
from .application import Command, Context
from .fields import (
    Field,
    Row,
    actor_key,
    decode_integer,
    enum_field,
    id_field,
    json_field,
    number,
    optional_id,
    optional_text,
    text_field,
)
from .records import Entity

POLICY = {
    "store.read": "reader",
    "store.upgrade": "manager",
    "team.read": "reader",
    "team.write": "manager",
    "team.list": "manager",
    "audit.read": "operator",
    "audit.export": "manager",
    "bundle.read": "reader",
    "bundle.write": "manager",
    "records.read": "reader",
    "records.write": "operator",
    "desk.report": "reader",
    "desk.export": "reader",
}
EVENTS = {"store.upgraded", "team.added", "team.suspended", "bundle.imported"}


def describe(ctx: Context) -> Row:
    return ctx.store.describe()


def commands(entities: Sequence[Entity]) -> tuple[Command, ...]:
    actor = Field("actor", actor_key, "desk member login")
    after = Field("after", number(), "exclusive audit cursor", default=0, cli=decode_integer)
    limit = Field("limit", number(1, 1000), "maximum audit rows", default=100, cli=decode_integer)
    filters = (
        optional_text("actor", "filter on actor"),
        optional_text("event", "filter on event name"),
    )
    return (
        Command(
            "store",
            "describe",
            "GET",
            "/v1/store/describe",
            "store.read",
            "store.describe",
            "Describe workspace tables and upgrades",
            (),
            describe,
        ),
        Command(
            "store",
            "upgrade",
            "POST",
            "/v1/store/upgrade",
            "store.upgrade",
            "store.upgrade",
            "Upgrade one workspace history",
            (id_field("name", "history upgrade name"),),
            history.upgrade,
        ),
        Command(
            "team",
            "whoami",
            "GET",
            "/v1/team/whoami",
            "team.read",
            "team.whoami",
            "Show current desk member",
            (),
            team.whoami,
        ),
        Command(
            "team",
            "list",
            "GET",
            "/v1/team/list",
            "team.list",
            "team.list",
            "List members",
            (),
            team.members,
        ),
        Command(
            "team",
            "add",
            "POST",
            "/v1/team/add",
            "team.write",
            "team.add",
            "Add a desk team member",
            (
                actor,
                text_field("name", "display name"),
                enum_field("role", "desk role", "reader", "operator", "manager"),
            ),
            team.add_member,
        ),
        Command(
            "team",
            "suspend",
            "POST",
            "/v1/team/suspend",
            "team.write",
            "team.suspend",
            "Suspend a desk team member",
            (actor,),
            team.suspend_member,
        ),
        Command(
            "audit",
            "list",
            "GET",
            "/v1/audit/list",
            "audit.read",
            "audit.list",
            "Read audit entries",
            (after, limit, *filters),
            audit.page,
        ),
        Command(
            "audit",
            "export",
            "GET",
            "/v1/audit/export",
            "audit.export",
            "audit.export",
            "Export audit entries as CSV",
            filters,
            audit.export,
        ),
        Command(
            "bundle",
            "export",
            "GET",
            "/v1/bundle/export",
            "bundle.read",
            "bundle.export",
            "Export an exchange bundle",
            (optional_id("table", "export one table"),),
            partial(exchange.export, entities=entities),
        ),
        Command(
            "bundle",
            "import",
            "POST",
            "/v1/bundle/import",
            "bundle.write",
            "bundle.import",
            "Merge a version-one exchange bundle",
            (json_field("bundle", "version-one bundle"),),
            partial(exchange.import_rows, entities=entities),
        ),
    )
