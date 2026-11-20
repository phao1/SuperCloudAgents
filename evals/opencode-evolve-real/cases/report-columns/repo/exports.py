import json

from projection import project


def csv_text(rows: list[dict], columns: list[str] | None = None, locale: str = "en") -> str:
    report = project(rows)
    return "".join(",".join(map(str, row)) + "\n" for row in (report.headers, *report.rows))


def json_text(rows: list[dict], columns: list[str] | None = None, locale: str = "en") -> str:
    return json.dumps(project(rows).rows)
