# Command guide

Each command starts with `python -B -m stockroom --db DATABASE`. Successful
commands print JSON followed by LF unless CSV was selected. An expected error
returns exit code 2, prints one `error: MESSAGE` line on stderr and leaves
stdout empty. Input can contain Unicode in titles, notes and memos. Public
codes use 1–24 ASCII letters, digits or hyphens and begin with a letter or
digit. Codes are stored uppercase. Dates use the form YYYY-MM-DD.

## Catalog and stock

```text
warehouse add CODE NAME
warehouse list [--all]
warehouse status CODE active|inactive
supplier add CODE NAME [--contact CONTACT]
supplier list [--all]
supplier status CODE active|inactive
item add SKU NAME [--unit UNIT] [--category CATEGORY]
item show SKU
item list [--category CATEGORY] [--all]
item status SKU active|inactive

lot receive SKU WAREHOUSE LOT QUANTITY DAY [--supplier CODE] [--memo TEXT]
lot adjust SKU WAREHOUSE LOT DELTA DAY [--memo TEXT]
lot move SKU SOURCE DESTINATION QUANTITY DAY [--memo TEXT]
lot ship SKU WAREHOUSE QUANTITY DAY [--memo TEXT]
lot list|export [--sku SKU] [--warehouse CODE] [--include-empty] [--format json|csv]
```

List commands suppress inactive catalog rows unless requested. An inactive
item may still have stock and may be corrected by an adjustment. Intake and
outgoing dispatch require active catalog records. Transfers need both
warehouses to be active. Stock movement quantities are whole units. Move and
ship take from oldest received lot first, using ID to break ties.

## Reports and notes

```text
report balances [--sku SKU] [--warehouse CODE] [--format json|csv]
report activity [--sku SKU] [--warehouse CODE] [--since DAY] [--through DAY]
                [--kind receive|adjust|transfer|ship] [--limit N] [--format json|csv]
report aging DAY [--sku SKU] [--warehouse CODE] [--format json|csv]
report trend [--period day|week|month] [--since DAY] [--through DAY]
             [--sku SKU] [--warehouse CODE] [--format json|csv]
report audit [--sku SKU] [--warehouse CODE] [--mismatches-only] [--format json|csv]
note add LOT_ID DAY TEXT
note list LOT_ID [--all]
note archive NOTE_ID
```

Report balances summarize positive lots only. Activity date bounds are
inclusive. Trend applies the filters before bucketing activity and includes
both signed sides of transfers. An ISO week bucket uses its Monday as its
label. Audit compares physical balances with the sum of movements for each lot.
Notes stay attached to a lot ID after quantity reaches zero.

## Counts and exchanges

```text
count open WAREHOUSE LABEL DAY
count record ID LOT_ID QUANTITY
count clear ID LOT_ID
count sheet ID [--differences-only] [--format json|csv]
count list [--warehouse CODE] [--status open|closed|aborted]
count close ID DAY
count abort ID DAY
file import-catalog PATH
file import-receipts PATH
file export-catalog [--all]
```

A stocktake starts with a point-in-time count sheet for each positive lot
in one warehouse. Record counted values then review the differences. Close
writes the corresponding stock adjustments in a single transaction. If the
counts are incomplete or quantities changed since opening, close fails and
keeps the sheet open. Closing day cannot precede opening day. Abort keeps
the sheet as a record without applying changes.

Catalog exchange accepts a JSON object with warehouses, suppliers and items
as optional arrays of objects. Import creates new entries only. Receipt
exchange takes CSV with sku, warehouse, code, quantity and received columns.
Supplier and memo columns are optional. The whole file is applied together.
See the change request for fields added to any of these commands.

## Order planning

Run `order plan PATH [--warehouse CODE ...]` with a JSON list such as
`[{"sku":"oil","quantity":5},{"sku":"rope","quantity":3}]`. Multiple requests
for the same SKU combine at the first occurrence. Each code in an explicit
warehouse list must be active and unique. The planner consumes preferred
warehouses in argument order, or all active warehouses in code order when
omitted. Within each warehouse it picks oldest received stock first.
The output has a `picks` array and a `shortages` array. Picks are proposals
only. They can be generated for partial orders and have no effect on stock
or the movement log.

## Historical balances

`report as-of DAY [--sku SKU] [--warehouse CODE] [--include-zero]
[--format json|csv]` reconstructs ledger balances through the requested day.
It includes only lots with movements through the cutoff and establishes order
by warehouse, SKU, receipt date and lot ID. The movement counter and latest
date are computed within the window. A backdated correction can result in
a negative historical balance when the corresponding receipt has a later date.
