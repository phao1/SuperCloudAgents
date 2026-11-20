# Workyard registers

A depot holds a catalog of tools and a separate member register. Reservations,
workshop logs, shared-pool orders and intake returns preserve the depot that
recorded the entry. Tools can be listed by home depot. A checkout from a
different depot uses the common reservation register.

`reservations queue` lists open due reservations by day and tool. The desk
availability projection counts open reservations and uses the tool's recorded
unit count. Workshop reports identify service allocations on the report date.

All records use per-register identifiers. A workspace may hold multiple
depots. Commands that work on one depot refer to its ID as
`scope`. Each dated register uses `day` for its entry date. Dates are inclusive
in reports and optional entry date filters.

| Register | Entry | Other fields |
| --- | --- | --- |
| `depots` | depot | name, region, capacity |
| `tools` | tool model | scope, name, kind, units, cents, status |
| `borrowers` | borrower | scope, name, tier, status |
| `reservations` | reservation | scope, day, status, tool, borrower, units, pickup, closed |
| `service-logs` | service log | scope, day, end, status, tool, units, outcome, result_day |
| `pool-orders` | pool order | scope, day, status, tool, units, substitute, closed |
| `returns` | return intake | scope, day, status, tool, borrower, units, damaged, rate, allowed, settled |
| `desk-memos` | paper memo | scope, day, message, author |

Each register provides `add`, `show`, `amend` and `list`. The list endpoint
accepts optional `scope` and `status` filters. `show` takes `id`. `amend` takes
`id` and a JSON object called `changes`. Identity fields and the fixed
references in each register are immutable through this generic command.
Business workflow commands are used for coordinated updates.

The fixture in `fixtures/workspace-v1.json` is a copy of the old register
layout, including open entries and historical paper records. Use a copy of
it when running examples.
