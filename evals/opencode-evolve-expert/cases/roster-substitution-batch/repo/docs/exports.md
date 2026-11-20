# Payroll and client feeds

Payroll rows have day, venue, member, minutes. This is a preview of
duty minutes after unpaid breaks, independent of member home zone.
Rows with no paid duty are omitted. Rows sort by civil date, then
by venue code and member code. Overlapping assignments are credited
independently; use the roster review to find such overlaps before
submitting payroll.

The legacy calendar export uses one VEVENT per scheduled shift within
the nominal date interval, including both ends. UIDs are the stable
shift IDs with @crewcall appended. DTSTART and DTEND are UTC. DTSTAMP
is the supplied export time. SUMMARY is the lane label. Text escapes
backslashes, newlines, commas and semicolons. Lines fold at 75 UTF-8
bytes with CRLF and one space before the continuation payload. The
feed byte order follows the ordered roster rows.
