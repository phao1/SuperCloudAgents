# Framehouse registers

Each production has a home venue and a minimum audience capacity. The booking
register represents day-long blocks, inclusive of both start and end day.
Crew records hold display names and duties. The attendance register stores one
day's worked minutes. The performance register contains audience lists from
the former ticket desk and an original booking when a performance was moved.

`bookings calendar` shows confirmed blocks that touch a requested interval.
`crew workload` sums attendance minutes by crew member over that interval.
The venue overview adds upcoming performances and attendance minutes for the
chosen day.

All records use per-register identifiers. A workspace may hold multiple
venues. Commands that work on one venue refer to its ID as
`scope`. Each dated register uses `day` for its entry date. Dates are inclusive
in reports and optional entry date filters.

| Register | Entry | Other fields |
| --- | --- | --- |
| `venues` | venue | name, district, capacity |
| `productions` | production | scope, name, seats, status |
| `crew` | crew member | scope, name, duty, status |
| `calls` | paper call | scope, day, status, production, crew, announced |
| `performances` | performance | scope, day, status, production, audience, prior_scope, prior_day, reason, sent |
| `bookings` | venue booking | scope, day, end, status, production, trade, former_scope, swapped |
| `attendance` | attendance mark | scope, day, status, production, person, minutes, original, reason, reviewed |
| `desk-memos` | paper memo | scope, day, message, author |

Each register provides `add`, `show`, `amend` and `list`. The list endpoint
accepts optional `scope` and `status` filters. `show` takes `id`. `amend` takes
`id` and a JSON object called `changes`. Identity fields and the fixed
references in each register are immutable through this generic command.
Business workflow commands are used for coordinated updates.

The fixture in `fixtures/workspace-v1.json` is a copy of the old register
layout, including open entries and historical paper records. Use a copy of
it when running examples.
