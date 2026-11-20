# Groveboard registers

A market has local suppliers and a SKU catalog. Orders snapshot unit prices
in cents. Staff log delivery quantities and record paper crate movements.
Buyers track dated supplier agreements, even when a subsequent agreement
replaces the old terms. The daybook and the CSV export carry these registers
between desks.

`purchase-orders pipeline` groups open orders by supplier. The market's
`desk report` rolls up draft order value, delivered units and the paper crate
balance by supplier. Only entries through the requested day are included.

All records use per-register identifiers. A workspace may hold multiple
markets. Commands that work on one market refer to its ID as
`scope`. Each dated register uses `day` for its entry date. Dates are inclusive
in reports and optional entry date filters.

| Register | Entry | Other fields |
| --- | --- | --- |
| `markets` | market | name, region, capacity |
| `suppliers` | supplier | scope, name, terms, status |
| `products` | produce SKU | scope, name, supplier, cents, status |
| `purchase-orders` | purchase order | scope, day, status, supplier, product, units, cents, round, released |
| `delivery-lines` | delivery line | scope, day, status, product, ordered, received, cents, credit, credited |
| `crate-moves` | crate movement | scope, day, status, supplier, kind, units, rate, cycle |
| `agreements` | supplier agreement | scope, day, end, status, supplier, cents, previous, activated |
| `desk-memos` | paper memo | scope, day, message, author |

Each register provides `add`, `show`, `amend` and `list`. The list endpoint
accepts optional `scope` and `status` filters. `show` takes `id`. `amend` takes
`id` and a JSON object called `changes`. Identity fields and the fixed
references in each register are immutable through this generic command.
Business workflow commands are used for coordinated updates.

The fixture in `fixtures/workspace-v1.json` is a copy of the old register
layout, including open entries and historical paper records. Use a copy of
it when running examples.
