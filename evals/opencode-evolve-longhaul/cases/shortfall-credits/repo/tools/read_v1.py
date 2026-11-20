"""Standalone reader shipped with the first desk release."""

import argparse
import json
from pathlib import Path


def read_table(path: Path, table: str) -> list[dict]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["format"] != 1:
        raise ValueError("this reader requires workspace format 1")
    rows = state["tables"].get(table, {})
    for record_id, row in rows.items():
        if row["id"] != record_id or row["version"] != 1:
            raise ValueError("this reader requires version-one records")
    return [rows[key] for key in sorted(rows)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("table")
    args = parser.parse_args()
    print(json.dumps(read_table(args.workspace, args.table), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
