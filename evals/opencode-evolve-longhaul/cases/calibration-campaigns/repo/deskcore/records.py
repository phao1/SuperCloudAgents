"""Legacy record operations used by the domain's registered catalogs."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .application import Command, Context
from .errors import fail
from .fields import Field, Row, bind, id_field, json_field, optional_id, optional_text
from .query import selected
from .storage import Store, Table

type Validate = Callable[[Store, Row], None]


def no_extra_validation(store: Store, row: Row) -> None:
    pass


@dataclass(frozen=True)
class Entity:
    name: str
    label: str
    french: str
    fields: tuple[Field, ...]
    validate: Validate = no_extra_validation
    immutable: tuple[str, ...] = ()

    @property
    def table(self) -> Table:
        return Table(self.name)

    def normalize(self, store: Store, row: Row) -> Row:
        optional = {field.name for field in self.fields if not field.required}
        supplied = {
            name: value for name, value in row.items() if value is not None or name not in optional
        }
        result = bind((id_field(), *self.fields), supplied)
        self.validate(store, result)
        return result

    def register(self, ctx: Context, **values) -> Row:
        row = self.normalize(ctx.store, values)
        stored = ctx.store.insert(self.name, row)
        ctx.audit(f"{self.name}.added", stored["id"])
        return stored

    def amend(self, ctx: Context, id: str, changes: Row) -> Row:
        before = ctx.store.require(self.name, id)
        if not changes:
            fail("bad_request", "supply at least one change")
        if {"version", "id", *self.immutable} & changes.keys():
            fail("bad_request", "fields cannot be amended")
        after = self.normalize(ctx.store, {**without_version(before), **changes})
        if before != {**after, "version": 1}:
            ctx.store.put(self.name, after)
            ctx.audit(f"{self.name}.amended", id, fields=sorted(changes))
        return ctx.store.require(self.name, id)

    def show(self, ctx: Context, id: str) -> Row:
        return ctx.store.require(self.name, id)

    def list(self, ctx: Context, scope: str | None, status: str | None) -> list[Row]:
        return selected(ctx.store.rows(self.name), scope=scope, status=status)

    def commands(self) -> tuple[Command, ...]:
        group = self.name
        route = f"/v1/{group}"
        return (
            Command(
                group,
                "add",
                "POST",
                route + "/add",
                "records.write",
                f"{group}.add",
                f"Register a {self.label}",
                (id_field(), *self.fields),
                self.register,
            ),
            Command(
                group,
                "amend",
                "POST",
                route + "/amend",
                "records.write",
                f"{group}.amend",
                f"Amend a {self.label}",
                (id_field(), json_field("changes", "fields to amend")),
                self.amend,
            ),
            Command(
                group,
                "show",
                "GET",
                route + "/show",
                "records.read",
                f"{group}.show",
                f"Show a {self.label}",
                (id_field(),),
                self.show,
            ),
            Command(
                group,
                "list",
                "GET",
                route + "/list",
                "records.read",
                f"{group}.list",
                f"List {group}",
                (
                    optional_id("scope", "filter by desk scope"),
                    optional_text("status", "filter by record status"),
                ),
                self.list,
            ),
        )


def without_version(record: Row) -> Row:
    return {name: value for name, value in record.items() if name != "version"}


def entity_commands(entities: Iterable[Entity]) -> list[Command]:
    return [command for entity in entities for command in entity.commands()]


def entity_events(entities: Iterable[Entity]) -> set[str]:
    return {f"{entity.name}.{suffix}" for entity in entities for suffix in ("added", "amended")}


def entity_messages(entities: Iterable[Entity], locale: str) -> dict[str, str]:
    phrases = (
        {"add": "Ajout", "amend": "Modification", "show": "Fiche", "list": "Liste"}
        if locale == "fr"
        else {"add": "Registered", "amend": "Amended", "show": "Record", "list": "Listing"}
    )
    return {
        f"{entity.name}.{verb}": f"{phrase} · {entity.french if locale == 'fr' else entity.label}"
        for entity in entities
        for verb, phrase in phrases.items()
    }
