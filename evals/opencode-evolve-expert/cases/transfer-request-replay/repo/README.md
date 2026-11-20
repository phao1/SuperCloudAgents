# Stockroom

Stockroom is a local inventory ledger for depots that share item codes but
track their stock independently. Use Python 3.12 and a local SQLite file.
Commands create a new store on the first open. Run them from this repository.

```bash
python -B -m stockroom --db depot.sqlite warehouse add west "West depot"
python -B -m stockroom --db depot.sqlite supplier add coop "Regional co-op"
python -B -m stockroom --db depot.sqlite item add oil "Lamp oil" --unit bottle
python -B -m stockroom --db depot.sqlite lot receive oil west parcel-1 12 2024-05-01 --supplier coop
python -B -m stockroom --db depot.sqlite report balances
```

- [Command guide](docs/commands.md)
- [Storage and transaction rules](docs/storage.md)
- [Current change request](docs/feature.md)

The existing regression suite runs with `python -B -m unittest discover -s tests`.
The task verifier is `python -B verify.py`.
