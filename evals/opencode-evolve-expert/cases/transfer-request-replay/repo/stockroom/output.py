"""Stable JSON and CSV representations for command output."""

import csv
import io
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, fields


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def object_json(model) -> str:
    return json_text(asdict(model))


def rows_json(values: Iterable) -> str:
    return json_text([asdict(row) for row in values])


def rows_csv(values: Iterable, model_type, columns: Sequence[str] | None = None) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    names = [field.name for field in fields(model_type)] if columns is None else list(columns)
    writer.writerow(names)
    for row in values:
        writer.writerow(["" if getattr(row, name) is None else getattr(row, name) for name in names])
    return stream.getvalue()


def render(values: Iterable, model_type, output_format: str) -> str:
    if output_format == "csv":
        return rows_csv(values, model_type)
    return rows_json(values)
