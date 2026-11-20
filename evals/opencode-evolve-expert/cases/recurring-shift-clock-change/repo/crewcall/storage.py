"""Read old desk snapshots and commit new ones as a single replacement."""

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .errors import DeskError
from .model import Assignment, Break, Lane, Member, Override, Rule, State, Venue


def read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, ValueError):
        raise DeskError(f"cannot read {path.name}") from None


def write(path: Path, value) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", dir=path.parent, prefix=f".{path.name}-", encoding="utf-8", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise DeskError(f"cannot write {path.name}") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _member(code: str, row: dict) -> Member:
    return Member(code, row["name"], tuple(row["roles"]))


def _rule(code: str, row: dict) -> Rule:
    return Rule(code, row["lane"], tuple(row["weekdays"]), row["start"],
                row["minutes"], row["first"], row.get("through"))


def load(path: Path) -> State:
    value = read(path, {})
    try:
        return State(
            venues={code: Venue(**row) for code, row in value.get("venues", {}).items()},
            members={code: _member(code, row) for code, row in value.get("members", {}).items()},
            lanes={code: Lane(**row) for code, row in value.get("lanes", {}).items()},
            rules={code: _rule(code, row) for code, row in value.get("rules", {}).items()},
            overrides={key: Override(**row) for key, row in value.get("overrides", {}).items()},
            assignments=tuple(Assignment(**row) for row in value.get("assignments", [])),
            breaks=tuple(Break(**row) for row in value.get("breaks", [])),
        )
    except (AttributeError, TypeError, KeyError, ValueError):
        raise DeskError("invalid desk state") from None


def save(path: Path, state: State) -> None:
    write(path, asdict(state))


def text(value) -> str:
    def convert(item):
        if isinstance(item, datetime):
            return item.isoformat().replace("+00:00", "Z")
        return asdict(item)

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=convert) + "\n"
