"""Dispatcher used by the in-process request API and the desk console."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .access import Principal, authorize
from .catalog import catalogs, error_message, message
from .errors import STATUS, DeskError, fail
from .fields import Field, Row, actor_key, bind, object_fields
from .storage import Store, Table

type Handler = Callable[..., Any]
type Upgrade = Callable[[Store], Iterable[Row]]


@dataclass(frozen=True)
class Command:
    group: str
    verb: str
    method: str
    path: str
    permission: str
    message: str
    help: str
    fields: tuple[Field, ...]
    handler: Handler

    @property
    def key(self) -> tuple[str, str]:
        return self.group, self.verb


@dataclass(frozen=True)
class Reply:
    status: int
    body: Row


@dataclass(frozen=True)
class Context:
    store: Store
    principal: Principal
    app: "Application"

    def audit(self, event: str, record_id: str, **details: Any) -> Row:
        if event not in self.app.events:
            fail("bad_request", f"unknown audit event: {event}")
        return self.store.append_event(self.principal.actor, event, record_id, details)


class Registry:
    def __init__(self, commands: Iterable[Command]) -> None:
        self.by_name: dict[tuple[str, str], Command] = {}
        self.by_route: dict[tuple[str, str], Command] = {}
        for item in commands:
            route = item.method, item.path
            if item.key in self.by_name or route in self.by_route:
                raise ValueError(f"duplicate command: {item.key} or {route}")
            if item.method not in ("GET", "POST") or not item.path.startswith("/v1/"):
                raise ValueError(f"unsupported route: {route}")
            if len({field.name for field in item.fields}) != len(item.fields):
                raise ValueError(f"duplicated option: {item.key}")
            self.by_name[item.key] = item
            self.by_route[route] = item

    def __iter__(self):
        return iter(self.by_name.values())


class Application:
    def __init__(
        self,
        commands: Iterable[Command],
        schema: Iterable[Table],
        policy: Mapping[str, str],
        messages: Mapping[str, Mapping[str, str]],
        events: Iterable[str],
        upgrades: Mapping[str, Upgrade],
    ) -> None:
        self.registry = Registry(commands)
        self.schema = {table.name: table for table in schema}
        self.policy = dict(policy)
        self.messages = catalogs(messages)
        self.events = frozenset(events)
        self.upgrades = dict(upgrades)

    def request(
        self,
        state: Path,
        actor: str,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        locale: str = "en",
    ) -> Reply:
        try:
            command = self.registry.by_route.get((method.upper(), path))
            if command is None:
                fail("not_found", f"{method} {path}")
            store = Store(state, self.schema)
            principal = authorize(
                actor_key(actor), store.member(actor), command.permission, self.policy
            )
            args = bind(command.fields, {} if params is None else object_fields(dict(params)))
            data = command.handler(Context(store, principal, self), **args)
            if store.changed and command.method != "POST":
                fail("bad_request", "read-only command changed state")
            store.flush()
        except OSError:
            return self.error(DeskError("io_error"), locale)
        except DeskError as error:
            return self.error(error, locale)
        return Reply(
            200, {"data": data, "message": message(self.messages, locale, command.message)}
        )

    def error(self, error: DeskError, locale: str) -> Reply:
        return Reply(
            STATUS[error.code],
            {
                "error": {
                    "code": error.code,
                    "message": error_message(error, self.messages, locale),
                    "detail": error.detail,
                }
            },
        )
