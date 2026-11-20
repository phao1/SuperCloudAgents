# Coverage board

'board coverage FIRST LAST [--venue VENUE]' lists one row per scheduled
shift in roster order. Rows include the lane, start and end, all crew
who still qualify for the lane, and any assigned crew who lost its role.
Crew codes sort alphabetically within a row. A shift is covered if at
least one assigned crew member still qualifies. Cancelled shifts are
omitted. 'board gaps' groups uncovered shifts by nominal day, venue
and role in that order. It reports unfilled shift IDs in roster order
and the total uncovered elapsed minutes before breaks. The board is
advisory and never removes assignments or writes the desk.
