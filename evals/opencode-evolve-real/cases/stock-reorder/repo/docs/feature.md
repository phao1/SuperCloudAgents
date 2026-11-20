# Reorder minimums

Provide an operator with a report of configured items below their minimum
on-hand stock. Each item and warehouse pair has its own optional rule.

## Writing configuration

- Add `item reorder-set SKU WAREHOUSE MINIMUM` and
  `Reporting.set_reorder(sku, warehouse, minimum)`. Normalize both codes
  using their existing field validators, then check that the item and
  warehouse exist, in that order. Both may be inactive. Use existing
  unknown item or warehouse errors. Minimum must be an actual int from
  0 through 2^63−1, inclusive. A bad numeric minimum raises
  `StockroomError("invalid minimum")` after code validation and lookup.
- Setting the same pair replaces just that rule. A minimum of 0 is a real
  configured value. Return a `models.ReorderRule` dataclass with fields
  `sku`, `warehouse`, `minimum` in that order. The command prints that
  object as ordinary JSON. The service writes no inventory movements.
- Add `item reorder-clear SKU WAREHOUSE` and
  `Reporting.clear_reorder(sku, warehouse) -> bool`. Use the same checks
  for the two codes. Delete just that rule. Return True for a removed rule,
  False for a genuinely absent rule. Print `{"removed":true}` or
  `{"removed":false}` in the CLI. Clearing does not touch stock.

## Reporting

Add `report reorder [--sku SKU] [--warehouse WAREHOUSE] [--all]
[--format json|csv]` and `Reporting.reorder(sku=None, warehouse=None,
include_healthy=False)`. Use existing filter normalization and combine any
two filters with AND. Unknown but valid filter codes give an empty report.
Show only configured pairs for which both item and warehouse are active.
Disabling either retains its configuration, and reactivating it can make
the row visible again.

Available stock is the sum of quantities of all current lots for the pair
(or zero if there are none). Use exact integer arithmetic even if the sum
exceeds 2^63−1. Calculate shortage as the greater of zero and minimum minus
available. Without `--all` (include_healthy=False), show only strictly
positive shortages. With `--all`, include all configured active pairs, also
those with minimum zero and those exactly at minimum. Order by warehouse
then SKU ascending, independent of insert order. Return a list of
`models.ReorderLine` dataclasses with fields `sku`, `warehouse`, `minimum`,
`available`, `shortage` in that order.

JSON is the ordinary array of objects, keeping all five integer and string
fields. CSV uses the same field order for the header and values, including
an empty report. Use the existing output conventions. The balances and
movement reports retain their existing meaning.

## Opening the store

Use SQLite user_version 2 for the new durable configuration. Existing
version 1 warehouses start with no rules and preserve their lot IDs,
catalog, and movement history. A fresh store must support the same
operations. Do not rewrite the original version 1 schema fixture.
