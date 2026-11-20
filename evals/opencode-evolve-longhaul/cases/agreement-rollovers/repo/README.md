# Groveboard

The supply desk for a group of neighborhood produce markets.

Run from this checkout with Python 3.12.

```sh
cp fixtures/workspace-v1.json /tmp/groveboard.json
python -B -m groveboard --state /tmp/groveboard.json desk report --scope north --day 2025-04-10
python -B -m groveboard --state /tmp/groveboard.json purchase-orders list --scope north
python -B tools/read_v1.py /tmp/groveboard.json purchase-orders
python -B verify.py
```

The sample workspace contains records for North Arcade and a second desk. See
[the operator guide](docs/operations.md), [the original registers](docs/registers.md)
and [development conventions](docs/development.md).
