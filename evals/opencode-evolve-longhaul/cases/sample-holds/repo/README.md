# Watershed

A field desk for physical water monitoring and station operations.

Run from this checkout with Python 3.12.

```sh
cp fixtures/workspace-v1.json /tmp/watershed.json
python -B -m watershed --state /tmp/watershed.json desk report --scope north --day 2025-04-10
python -B -m watershed --state /tmp/watershed.json visits list --scope north
python -B tools/read_v1.py /tmp/watershed.json visits
python -B verify.py
```

The sample workspace contains records for North Weir and a second desk. See
[the operator guide](docs/operations.md), [the original registers](docs/registers.md)
and [development conventions](docs/development.md).
