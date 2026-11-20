# Watershed registers

Each station has a home crew and a hardware register. A visit has a lead
crew and a position in the station's field log. Meter readings are integer
offsets in the units configured for the meter. Crews file station incidents
and collect water samples for physical measurements. All daybook dates are
calendar dates.

`visits itinerary` lists scheduled station visits for a crew in order.
`meters tolerances` lists recent signed meter deviations. The station
overview includes outstanding incidents and sample counts for the given day.

All records use per-register identifiers. A workspace may hold multiple
stations. Commands that work on one station refer to its ID as
`scope`. Each dated register uses `day` for its entry date. Dates are inclusive
in reports and optional entry date filters.

| Register | Entry | Other fields |
| --- | --- | --- |
| `stations` | monitoring station | name, district, capacity |
| `crews` | field crew | scope, name, lead, status |
| `meters` | field meter | scope, name, kind, tolerance, status |
| `visits` | field visit | scope, day, status, crew, position, route, dispatched |
| `calibration-logs` | calibration reading | scope, day, status, meter, reading, campaign, reviewed |
| `incidents` | station incident | scope, day, status, crew, priority, former, received, due |
| `samples` | sample record | scope, day, status, water, hold, held, resolved, note |
| `desk-memos` | paper memo | scope, day, message, author |

Each register provides `add`, `show`, `amend` and `list`. The list endpoint
accepts optional `scope` and `status` filters. `show` takes `id`. `amend` takes
`id` and a JSON object called `changes`. Identity fields and the fixed
references in each register are immutable through this generic command.
Business workflow commands are used for coordinated updates.

The fixture in `fixtures/workspace-v1.json` is a copy of the old register
layout, including open entries and historical paper records. Use a copy of
it when running examples.
