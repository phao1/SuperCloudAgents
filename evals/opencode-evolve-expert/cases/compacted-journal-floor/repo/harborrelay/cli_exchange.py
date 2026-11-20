"""Pack-file entry points used by desks doing an offline handoff."""

from dataclasses import asdict
from pathlib import Path

from .audit import archive as read_archive
from .audit import audit_pack, difference
from .exchange import dump, load
from .journal import Journal


def register(commands) -> None:
    archive = commands.add_parser("archive", help="import or export packs")
    actions = archive.add_subparsers(dest="job", required=True)
    export = actions.add_parser("export")
    export.add_argument("path", type=Path)
    export.add_argument("--after", type=int)
    export.set_defaults(action=export_pack)
    restore = actions.add_parser("import")
    restore.add_argument("path", type=Path)
    restore.set_defaults(action=import_pack)
    inspect = actions.add_parser("inspect")
    inspect.add_argument("path", type=Path)
    inspect.set_defaults(action=inspect_pack)
    diff = actions.add_parser("diff")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    diff.set_defaults(action=diff_packs)


def export_pack(home, args) -> dict:
    return {"exported": dump(Journal(home), args.path, args.after)}


def import_pack(home, args) -> dict:
    return {"sequences": [row.seq for row in load(Journal(home), args.path)]}


def inspect_pack(home, args) -> dict:
    return asdict(audit_pack(read_archive(args.path)))


def diff_packs(home, args) -> dict:
    return asdict(difference(read_archive(args.left), read_archive(args.right)))
