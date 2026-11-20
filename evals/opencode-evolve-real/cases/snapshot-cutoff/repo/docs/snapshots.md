# Exporting a point-in-time snapshot

The export command prepares a report as of, but excluding, a supplied cutoff.
A reading exactly at the cutoff is ineligible regardless of its written UTC
offset. Readings on either side of the cutoff can differ by one microsecond.
The time index compares instants from the full datetime range used here
(years 2 through 9998) without dropping precision. Provided datetimes are
aware, and their fixed offsets are integral minutes. The machine's time zone
plays no role.

The report prints `id=value` per line and ends each line with LF. Order is
earliest to latest instant, preserving input order among ties. If a cutoff
actually excludes every reading, `report.render` still raises
`RuntimeError("snapshot is empty")`. `report.render` and `readings.before`
must see the same cutoffs. `keys.instant_key` documents the numeric index
format, which is also consumed by a snapshot importer. Do not mutate or sort
the caller's collection in place.
