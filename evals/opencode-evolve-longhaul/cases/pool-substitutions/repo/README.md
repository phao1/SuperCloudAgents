# Workyard

A tool-lending desk for a network of neighborhood workshops.

Run from this checkout with Python 3.12.

```sh
cp fixtures/workspace-v1.json /tmp/workyard.json
python -B -m workyard --state /tmp/workyard.json desk report --scope north --day 2025-04-10
python -B -m workyard --state /tmp/workyard.json reservations list --scope north
python -B tools/read_v1.py /tmp/workyard.json reservations
python -B verify.py
```

The sample workspace contains records for North Yard and a second desk. See
[the operator guide](docs/operations.md), [the original registers](docs/registers.md)
and [development conventions](docs/development.md).
