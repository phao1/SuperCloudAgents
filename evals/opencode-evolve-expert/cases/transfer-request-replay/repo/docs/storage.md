# From commands to rows

The command modules register arguments and construct request models. Catalog,
Inventory, Reporting, Notes and Counts implement the business service APIs.
The row classes contain SQL against the shared schema. Services handle
normalization, transactions and the conversion of recoverable database
conditions to command errors. The output module owns JSON and CSV wire
formats. JSON keeps Unicode and compact whitespace. CSV has a header and
uses CRLF terminators, escaping CSV delimiters, quotes and embedded line breaks.

## Identity and history

An item SKU or warehouse code identifies a catalog row. The tuple
(sku, warehouse, code) identifies one stock lot. It has a stable numeric ID
which is referenced by adjustment history and notes. Supplier and receipt
day are the source metadata. A later receipt with the same lot identity
adds to the quantity only if its source metadata agrees. A warehouse
transfer carries the source metadata to the destination and writes two
movement records with opposite quantities. If the destination already has
the lot, receipt metadata must match there too.

Movement rows are ordered by the dates the operator supplied, then the
autoincrementing IDs from their insertion order. The date on a receipt
sets its FIFO position regardless of when it was entered. Corrections
preserve the original lot ID. Quantity zero is a useful historical lot
and is omitted from `lot list` unless the caller requests it. Audit reports
include depleted lots.

## Version 1

`stockroom/schema.sql` describes the original SQLite layout and is also
a fixture for reproducing a database already deployed in the field.
`db.open_store` opens a database and enables foreign keys on each connection.
If SQLite reports user_version 0, it applies the original SQL. Additional
versions can be reached by incremental migrations in the database layer.
Existing files and primary keys must survive future migrations. Databases
from newer, unknown versions must fail with a clear command error.

## Service transactions

Methods that modify data use the connection's transaction context. Row
classes let the caller own that transaction. A failure in a later receipt,
lot transfer or count adjustment rolls back the earlier writes in the same
operation. A service method that returns after a write should leave the
transaction committed so another process can see the same data. Reports
and lookups use the current data even after the original connection is
closed and reopened.
