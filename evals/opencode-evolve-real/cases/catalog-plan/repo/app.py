from graph import Package, traverse
from output import format_plan


def build(targets: list[str], records: dict[str, Package], aliases: dict[str, str]) -> dict:
    packages, skipped = traverse(targets, records, aliases)
    return format_plan(packages, skipped)
