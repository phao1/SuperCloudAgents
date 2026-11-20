"""Legacy registers and their validated fields, in dependency order."""

from deskcore.fields import (
    date_field,
    enum_field,
    id_field,
    int_field,
    optional_date,
    optional_id,
    optional_text,
    text_field,
)
from deskcore.records import Entity

from . import validation

ENTITIES = (
    Entity(
        "stations",
        "monitoring station",
        "station",
        (
            text_field("name", "public name"),
            text_field("district", "catchment name"),
            int_field("capacity", "daily visit capacity", 1, 10000000),
        ),
    ),
    Entity(
        "crews",
        "field crew",
        "équipe de terrain",
        (
            id_field("scope", "desk scope"),
            text_field("name", "public name"),
            id_field("lead", "desk member heading the crew"),
            enum_field("status", "register status", "active", "inactive"),
        ),
        validation.require_scope,
        ("scope",),
    ),
    Entity(
        "meters",
        "field meter",
        "instrument",
        (
            id_field("scope", "desk scope"),
            text_field("name", "public name"),
            id_field("kind", "measurement type"),
            int_field("tolerance", "maximum absolute reading deviation", 0, 10000000),
            enum_field("status", "register status", "active", "retired"),
        ),
        validation.require_scope,
        ("scope", "kind"),
    ),
    Entity(
        "visits",
        "field visit",
        "visite",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "scheduled", "dispatched", "cancelled", "done"),
            id_field("crew", "field crew"),
            int_field("position", "visit position", 1, 10000000),
            optional_id("route", "paper route"),
            optional_date("dispatched", "route dispatch date"),
        ),
        validation.visit,
        ("scope", "crew"),
    ),
    Entity(
        "calibration-logs",
        "calibration reading",
        "étalonnage",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "logged", "reviewed", "void"),
            id_field("meter", "meter"),
            int_field("reading", "signed deviation", -100000, 10000000),
            optional_id("campaign", "paper campaign ID"),
            optional_date("reviewed", "review date"),
        ),
        validation.calibration,
        ("scope", "meter"),
    ),
    Entity(
        "incidents",
        "station incident",
        "incident",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "open", "forwarded", "closed"),
            id_field("crew", "responsible crew"),
            enum_field("priority", "field priority", "normal", "urgent"),
            optional_id("former", "prior crew"),
            optional_date("received", "receiving day"),
            optional_date("due", "response deadline"),
        ),
        validation.incident,
        ("scope",),
    ),
    Entity(
        "samples",
        "sample record",
        "échantillon",
        (
            id_field("scope", "desk scope"),
            date_field("day", "entry day"),
            enum_field("status", "register status", "logged", "cleared", "discarded"),
            id_field("water", "physical sample type"),
            optional_id("hold", "paper hold"),
            optional_date("held", "hold placed on"),
            optional_date("resolved", "hold resolved on"),
            optional_text("note", "reason for a hold"),
        ),
        validation.require_scope,
        ("scope", "water"),
    ),
)
