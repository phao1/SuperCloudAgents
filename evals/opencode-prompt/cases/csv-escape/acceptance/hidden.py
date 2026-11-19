import csv
from io import StringIO
from pathlib import Path

from app import export_name, render_row

for fields in [['a"b', "c\rd", "e\nf", ""], [""], ["plain", "last"]]:
    text = render_row(iter(fields))
    assert text.endswith("\r\n")
    assert list(csv.reader(StringIO(text, newline=""))) == [fields]
assert render_row(['a"b']) == '"a""b"\r\n'
assert export_name("snapshot.3") == "snapshot.3.csv"
assert Path("verify.py").read_text() == (
    'from app import render_row\n\n'
    'assert render_row(["alpha,beta", "gamma"]) == \'"alpha,beta",gamma\\r\\n\'\n'
)
