from copy import deepcopy
from pathlib import Path

from app import summarize, unique_records

original = [
    {"id": "b", "tags": ["first"]},
    {"id": "A", "tags": []},
    {"id": "b", "tags": ["second"]},
    {"id": "a", "tags": [1]},
]
snapshot = deepcopy(original)
assert unique_records(iter(original)) == [snapshot[0], snapshot[1], snapshot[3]]
assert original == snapshot
assert unique_records(iter([])) == []
assert summarize(original) == 4
assert Path("verify.py").read_text() == (
    'from app import unique_records\n\n'
    'assert unique_records([{"id": "x", "amount": 1}, {"id": "x", "amount": 2}]) == [\n'
    '    {"id": "x", "amount": 1},\n'
    ']\n'
)
