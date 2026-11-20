# Desk commands

Put --desk PATH ahead of the command. Expected errors return code 2,
one 'error: MESSAGE' diagnostic, and no output. JSON output is
line-terminated UTF-8. Calendar commands emit CRLF iCalendar text.
Dates are ISO calendar dates. Clock readings use 24-hour HH:MM.
Role, venue, member, lane and rule codes are case-sensitive ASCII
identifiers. Display names retain punctuation and non-ASCII text.

    venue put CODE NAME ZONE
    venue list
    member put CODE NAME ROLE [ROLE ...]
    member list
    lane put CODE VENUE ROLE LABEL
    lane list [--venue VENUE]
    rule put CODE LANE WEEKDAYS START MINUTES FIRST [--through LAST]
    rule list
    rule change CODE DAY [--skip | --start CLOCK] [--minutes MINUTES]
    roster show FIRST LAST [--venue VENUE]
    roster assign SHIFT MEMBER
    roster unassign SHIFT MEMBER
    roster crew SHIFT
    roster break SHIFT MEMBER OFFSET MINUTES
    roster conflicts MEMBER SINCE THROUGH
    payroll report FIRST LAST [--venue VENUE]
    calendar export FIRST LAST --at UTC

WEEKDAYS is a comma-separated list using mon,tue,wed,thu,fri,sat,sun
with no repeats. SINCE, THROUGH and --at take UTC ISO timestamps
with whole minutes or whole seconds (a Z or +00:00 suffix).
The calendar command is a read-only snapshot in the legacy release.

Use the [coverage board](coverage.md) for vacancy and role-review reports.
