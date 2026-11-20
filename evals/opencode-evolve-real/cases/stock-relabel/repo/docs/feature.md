# Durable SKU renames

Operators occasionally correct an item code. A correction must keep the
item's history and let existing order files use the old code. Keep all
version 1 stores usable and preserve the behavior of unrenamed items.

## Catalog commands and service

- Add `item rename OLD NEW`, backed by `Catalog.rename_item(sku, new_sku)`.
  Both arguments use existing SKU normalization and validation. Normalize
  and validate both before looking up the source. The source can be an
  existing canonical SKU or an alias. An unknown source raises the
  existing `unknown sku` error. If NEW is already the source item's
  current canonical code after normalization, return that item without
  a write or a new alias. If NEW is any other canonical SKU or any
  existing alias, raise the existing `sku already exists` conflict.
- Renaming otherwise replaces only the canonical SKU. Preserve other
  catalog fields and make the previous canonical SKU a permanent alias.
  All earlier aliases keep working with the new canonical SKU. Renames
  affect inactive items too. A failed operation makes no partial change.
  A successful command prints the existing Item JSON shape using the
  new canonical SKU and no extra fields.
- Add `item aliases SKU` and `Catalog.aliases(sku) -> list[str]`.
  Lookup accepts a canonical or alias SKU, follows existing validation
  and missing errors, and returns all aliases of that item sorted by
  code. The command prints `{"sku":"CURRENT","aliases":[...]}`. An item
  with no aliases gives an empty list.
- Aliases reserve their codes permanently. `item add`, service creation
  and JSON catalog import reject those codes as `sku already exists`,
  including rolling back any prior records in the same import.

## Store and request boundaries

On open, migrate version 1 SQLite stores to user_version 2 while
preserving all existing data. Initialize fresh stores to the same
version. The original schema file is the version 1 fixture.

After rename, lot and movement rows must reference the new canonical
SKU. Preserve lot IDs, notes, movement IDs and their quantities and
dates. Existing stocktake sheets must still show the canonical SKU.
Do not record a synthetic stock movement for a rename.

Use aliases and current codes interchangeably for item show and status,
all stock operations (including receipt CSV), lot list and export filters,
all report SKU filters, and fulfillment requests. Where a command or
service previously treated an unknown but valid SKU filter as an empty
selection, retain that behavior. Combining orders for old and new codes
must produce one demand for that item. All existing outputs show
canonical codes, including CSV and JSON, and catalog list/export contain
only canonical items. Apart from the new commands, wire shapes stay
the same. Warehouse, supplier and lot codes are independent namespaces.
