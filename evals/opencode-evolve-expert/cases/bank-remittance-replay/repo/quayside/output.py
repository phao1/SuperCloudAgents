"""CSV is a projection of the same statement returned by the service."""

import csv
from dataclasses import astuple, fields
from io import StringIO

from .model import Entry, Statement


def csv_text(header, values) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(values)
    return output.getvalue()


def statement_csv(report: Statement) -> str:
    return csv_text([field.name for field in fields(Entry)], map(astuple, report.entries))
