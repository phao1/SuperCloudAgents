# Framehouse

An operations desk for venues that share productions and crew.

Run from this checkout with Python 3.12.

```sh
cp fixtures/workspace-v1.json /tmp/framehouse.json
python -B -m framehouse --state /tmp/framehouse.json desk report --scope north --day 2025-04-10
python -B -m framehouse --state /tmp/framehouse.json calls list --scope north
python -B tools/read_v1.py /tmp/framehouse.json calls
python -B verify.py
```

The sample workspace contains records for North Studio and a second desk. See
[the operator guide](docs/operations.md), [the original registers](docs/registers.md)
and [development conventions](docs/development.md).
