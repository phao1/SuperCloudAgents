# Running an offline desk

Use Python 3.12. No services or packages are required. Start with the sample
workspace under `fixtures/` and copy it to a writable place. For a new desk,
the first write creates the JSON file. With no flag, the file is `desk.json`.

Put `--state PATH`, `--actor NAME`, and `--locale en|fr` before the command
group. Use `GROUP --help` to list available actions. For API clients,
`request(workspace, actor, method, route, params, locale="en")` returns a
reply with a numeric `status` and a JSON-serializable `body`. Use the string
values shown in command help and pass typed numbers and lists to the API.

| Command | Use |
| --- | --- |
| `store describe` | See registered tables, row counts and upgrade receipts |
| `store upgrade --name bulletins` | Convert paper memos to bulletins |
| `team whoami` | Check the active member and role |
| `team add --actor pat --name Pat --role operator` | Add an operator as a manager |
| `team suspend --actor pat` | Suspend a member as a manager |
| `audit list --after 12 --limit 30` | Read a page of events as an operator |
| `audit export --actor pat` | Get CSV and a count as a manager |
| `desk report --scope north --day 2025-04-10` | Open the desk's report on a given day |
| `desk export --table desk-memos --scope north` | Export one dated register as CSV |
| `bundle export` | Read a version-one portable bundle |
| `bundle import --bundle '{...}'` | Merge a portable bundle as a manager |

Successful commands return a JSON object with `data` and a localized
`message`. Failures include a stable `error.code` and localized
`error.message`. CLI errors use stderr and a nonzero exit status.

For a move to another desk release, use `bundle export` for the legacy
registers. It carries stable IDs and record versions. Import merges records in
dependency order. An existing record is left in place if it equals the
incoming record. If a record conflicts, the entire import is aborted, and
the desk needs to resolve the difference before retrying. The bundle contains
no team members, audit events or version-two tables. Copy the workspace
file for a full backup.

## Reviewed intake

Large received bundles can go through a separate review before they are
merged. Use `intake prepare --id delivery-a --source "District office"
--bundle '{...}'` to save a proposed intake and preview the number of rows
that will be added or left unchanged. Preparation validates references and
will fail as a whole if the bundle is malformed or conflicts. The original
registers are untouched while review is pending.

Use `intake show --id delivery-a` or `intake list --status ready` to review the
summary. A manager can run `intake apply --id delivery-a` to commit the bundle
or `intake cancel --id delivery-a --reason "Revised copy expected"` to drop it.
If one of the proposed rows has changed locally since preparation, file a new
intake with a refreshed bundle.

## Follow-up queue

File one active follow-up against a legacy record at a time. For example,
`followups open --id call-back --register desk-memos --source memo-north
--owner clerk --day 2025-04-07 --due 2025-04-12 --priority normal
--note "Confirm pickup time"` places a task in the North queue. A manager
can change the owner with `followups assign --id call-back --owner lead`.
The owner or a manager can change the due date with `followups reschedule`
or use `followups close --id call-back --day 2025-04-12 --outcome "Confirmed"`.
The queue sorts by deadline, urgent work first on that day, then ID.
The desk overview uses its requested date to distinguish overdue items
from items due that day.
