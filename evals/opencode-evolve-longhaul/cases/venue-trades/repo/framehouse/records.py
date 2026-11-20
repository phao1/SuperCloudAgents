"""Legacy registers and their validated fields, in dependency order."""

from deskcore.fields import (
    date_field,
    enum_field,
    id_field,
    ids_field,
    int_field,
    optional_date,
    optional_id,
    optional_int,
    optional_text,
    text_field,
)
from deskcore.records import Entity

from . import validation

ENTITIES = (
    Entity(
        "venues",
        "venue",
        "salle",
        (
            text_field("name", "public name"),
            text_field("district", "district name"),
            int_field("capacity", "seating capacity", 1, 10000000),
        ),
    ),
    Entity(
        "productions",
        "production",
        "spectacle",
        (
            id_field("scope", "desk scope"),
            text_field("name", "public name"),
            int_field("seats", "minimum venue capacity", 1, 10000000),
            enum_field("status", "register status", "active", "archived"),
        ),
        validation.production,
        ("scope",),
    ),
    Entity(
        "crew",
        "crew member",
        "technicien",
        (
            id_field("scope", "desk scope"),
            text_field("name", "public name"),
            id_field("duty", "crew duty"),
            enum_field("status", "register status", "active", "inactive"),
        ),
        validation.require_scope,
        ("scope",),
    ),
    Entity(
        "calls",
        "paper call",
        "appel papier",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "draft", "announced", "cancelled"),
            id_field("production", "production"),
            ids_field("crew", "called crew IDs"),
            optional_date("announced", "publication date"),
        ),
        validation.call,
        ("scope", "production"),
    ),
    Entity(
        "performances",
        "performance",
        "représentation",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "scheduled", "moved", "cancelled"),
            id_field("production", "production"),
            ids_field("audience", "audience contact list IDs"),
            optional_id("prior_scope", "previous venue"),
            optional_date("prior_day", "previous performance day"),
            optional_text("reason", "explanation of the move"),
            optional_date("sent", "notice date"),
        ),
        validation.performance,
        ("production",),
    ),
    Entity(
        "bookings",
        "venue booking",
        "réservation de salle",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            date_field("end", "last calendar day"),
            enum_field("status", "register status", "confirmed", "cancelled"),
            id_field("production", "production"),
            optional_id("trade", "paper trade identifier"),
            optional_id("former_scope", "previous venue"),
            optional_date("swapped", "desk approval date"),
        ),
        validation.booking,
        ("production",),
    ),
    Entity(
        "attendance",
        "attendance mark",
        "présence",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "recorded", "corrected"),
            id_field("production", "production"),
            id_field("person", "crew member"),
            int_field("minutes", "worked minutes", 0, 1440),
            optional_int("original", "original worked minutes", 0),
            optional_text("reason", "reason for correction"),
            optional_date("reviewed", "correction reviewed on"),
        ),
        validation.attendance,
        ("scope", "production", "person"),
    ),
)
