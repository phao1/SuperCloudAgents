# Harbor Relay

Harbor Relay collects status events from depot desks that have
intermittent connectivity. It keeps a local journal and can route
events into volunteer worklists, produce summaries, or exchange a
bounded archive with another desk. Commands use a private work
directory and can run with no broker or network connection.

Run with Python 3.12 from this repository.

    python -B -m harborrelay --home ./desk publish dock.arrivals landed A2 '{"crates":4}' --at 22
    python -B -m harborrelay --home ./desk register morning 'dock.*'
    python -B -m harborrelay --home ./desk watch morning
    python -B -m harborrelay --home ./desk summary
    python -B -m harborrelay --home ./desk archive export morning.hr

See [command guide](docs/operator.md), [storage](docs/journal.md),
[worker notes](docs/workers.md) and [archive format](docs/exchange.md).
Read the accepted changes under docs/adr/ alongside older manuals.
Run the existing regression suite with python -B -m unittest discover -s tests.
