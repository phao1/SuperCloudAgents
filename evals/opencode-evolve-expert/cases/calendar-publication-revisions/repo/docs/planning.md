# Shift and assignment rules

A recurring shift is identified by RULE:YYYY-MM-DD, with the rule's
scheduled civil date at the venue. The end is measured using elapsed
minutes from its actual start. A one-off change can skip the shift
or override the starting wall time, duration, or both, leaving the
identity alone. Rules can be rewritten in place. The CLI sorts
displayed shifts by actual start, lane, then ID.

Each lane requires the named role. Several crew members can work
one shift, and one member can have several shifts, even if they
overlap. 'roster conflicts' is an advisory report. Assignments are
unique by shift and member. Removing an assignment discards its
recorded breaks. The break offsets and lengths use elapsed minutes
relative to shift start and must fit within the shift. Breaks may
overlap when two supervisors record the same interruption.

The desk file is written atomically and contains only requested
configuration, exceptions, assignments and breaks. Derived shifts
are recomputed after opening. A change that fails validation leaves
the previous desk file unchanged. A rule can skip an occurrence with
recorded assignments, which then remain on file for audit but do
not appear in scheduled output until it is restored.

Older wall-clock planning assumed the first local clock reading was
always usable. Sites with a changed clock should use the current
scheduling handoff. The supported test zones are stored as TZif data
in the application so the offline desk can reopen the same rules.
