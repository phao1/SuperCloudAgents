# Extending a desk

Each application assembles its own commands and registered tables and shares
the `deskcore` runtime. Keep business decisions in application modules that
take a `deskcore.application.Context`. The command module describes the fields
and maps the two entry points to one handler. A feature may call existing
application helpers with the same context.

## Registry, access and messages

Every command is exactly one `Command` in the application registry. For a
command `desk report`, the route is `GET /v1/desk/report`. Writes use `POST`.
Use the same handler for both entry points. The registry builds the terminal
parser and the in-process API route map. Use `Field` validators for shared input
parsing. JSON array and object arguments use JSON text on the command line.
Run the existing example `store describe --help` to see the terminal shape.

The policy map gives each command's permission key its minimum role. Additional
cross-record or member checks belong in the handler. The default seed members
are `viewer` (reader), `clerk` (operator) and `lead` (manager). Suspended and
unauthorized members cannot run the command. Audit writes use `ctx.audit` with
event keys registered in the application catalog, the relevant record ID, and
JSON-serializable details. Each successful change adds an event unless the
operation is an idempotent replay. Rejected operations leave the entire
workspace alone. Every command has entries in the English and French response
catalogs. Success messages come from the catalog. Errors use the common codes.

## Persistence and history

The first release stores a JSON workspace with `format: 1`, a `members` map,
`tables` for the legacy registers, and an `audit` list. Each table is a map of
record IDs to objects with `id` and `version: 1`. The old reader at
`tools/read_v1.py` reads this format directly. The legacy API and portable
exports must continue to expose those original records.

New workflows use `Table(name, 2)`. Each such table is stored in
`extensions[name]` with `schema: 2` and a `records` map. Every record has an
`id` and `version: 2`. Use `ctx.store.insert` and `ctx.store.put` to save
records. The runtime saves the whole workspace once after a successful write.
It keeps the root format at 1 and leaves unrelated tables and extensions in
place. `ctx.store.rows` returns rows in ascending ID order. Where a workflow
changes a legacy record, leave its version and legacy fields intact.

Register a pure converter in the application migration map under the new
table's name. `store upgrade --name <name>` (API `POST /v1/store/upgrade`) runs
it in a single write, sets its receipt in `upgrades`, and returns the number
of newly imported rows. Converters read existing records and return new rows,
leaving the source rows alone. The runner rejects historical ID collisions
with different existing records. A repeated upgrade reports zero and does
not rewrite the file or add an audit event. A converter should reject invalid
historical groupings with `bad_store`. See the bulletin converter and
`deskcore/history.py`.

Use deterministic record ordering and integer quantities. Read queries are
side-effect free. The runtime does not use the system date to infer business
dates. Add focused unittest tests and a short operator guide with English
command examples and an upgrade example. Use temporary workspace copies in
tests and leave the original fixtures unchanged.
