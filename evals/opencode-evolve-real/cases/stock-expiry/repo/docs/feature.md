# Expiry dates for lots

Some supplies have a known expiry date. Record it with the lot and let
operators request lots usable on a given day. Preserve all existing
regression behavior and legacy output.

## Intake and storage

- Append an optional `expires_on: str | None = None` field to both
  `models.Intake` and `models.Lot`. Existing positional callers remain
  valid. `Inventory.receive` and `receive_all` accept the new field.
- `lot receive` accepts `--expires-on DAY`. Receipt CSV intake adds an
  optional `expires_on` header, with an empty field or absent header
  meaning None. Use the existing batch transaction.
- The date, when supplied, uses the same calendar format and validation
  as the receipt day. A malformed or impossible date raises
  `StockroomError("invalid day")`. If it precedes the receipt day, raise
  `StockroomError("expiry before receipt")`. Equality is valid.
- Migrate existing version 1 databases to SQLite user_version 2 when
  opening them, retaining all existing rows, IDs and movements. Existing
  lots have None for expiry. Fresh databases use the same version 2
  layout. Keep the original version 1 schema file intact.
- Expiry is source metadata alongside receipt date and supplier. Two
  receipts to the same lot identity must agree exactly, including
  None. A mismatch produces the existing `lot details differ` error
  with no changed quantities or added movements. When moving stock,
  carry expiry from the source and require it to agree with any
  existing lot at the destination. If any piece conflicts, roll back
  the whole move. Shipping still uses the ordinary FIFO plan, with
  no implicit expiry filter. Other commands retain their behavior.

## Listing and exports

`Inventory.lots` and CLI `lot list` and `lot export` use optional
`usable_on` and `--usable-on DAY`, respectively. Validate it like a receipt
date. An eligible lot has `received <= usable_on` and no expiry or
`expires_on >= usable_on`. Intersect this filter with existing SKU,
warehouse and include-empty filters. Order stays as before.

Every existing JSON representation of a Lot includes `expires_on` only
when known (a date string). Legacy and other undated rows keep their old
shape. Both listing commands accept `--with-expiry`. It only affects CSV
format: append one trailing `expires_on` column, displaying undated lots
as a blank. It applies even if there are no rows. Without this option
keep the original CSV header and row layout exactly. Existing CSV quoting
and CRLF line terminators apply.

Example: `lot receive AX NORTH L4 3 2024-02-29 --expires-on 2024-03-01`
creates a lot visible to `lot list --usable-on 2024-03-01` and absent from
`lot list --usable-on 2024-03-02`.
