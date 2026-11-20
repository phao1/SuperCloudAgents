# Report columns

The `projection.project` entry point takes a list of input mappings, a list of
column names or None, and a locale (`en` or `zh`). It returns a Report
with tuples `headers`, `rows` (a tuple of tuples), and `warnings`.
`exports.csv_text` and `exports.json_text` accept the same arguments and
use the same projection. All three APIs use defaults `columns=None` and
`locale="en"`. Never modify input rows or selections.

With None select id, title and count in that order. An explicit selection
keeps the requested order and can be empty. Canonical names and aliases are
case-sensitive. Resolve each alias before checking uniqueness. Reject an
unknown column or a duplicate canonical column with
`ValueError("invalid columns")`. An unknown locale raises
`ValueError("invalid locale")`; check it before checking columns. Selections
in this fixture are lists of strings or None. The set of columns and cell
rules are specified by the docstrings in projection.py. See the ADR for
exact output conventions, including empty reports.
