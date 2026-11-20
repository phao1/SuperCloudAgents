from app import build
from graph import Package

assert build(["site"], {"site": Package("1", ["?theme"])}, {}) == {
    "packages": [{"name": "site", "version": "1", "selected_by": None}],
    "skipped": [{"parent": "site", "name": "theme"}],
}
