# Timeline compaction

Implement `app.compact(events, start, stop, minimum=1) -> list[tuple]` for
a planning timeline. It returns a list of `(start, stop, label)` tuples
describing the visible pieces inside [start, stop). Ticks are arbitrary
signed integers. Handle large tick ranges in terms of event boundaries.

Input is an iterable of `app.Segment(start, stop, label, priority=0)`
objects and can be consumed only once. Validate window arguments before
touching the iterable. All bounds and priorities are actual ints; booleans
are invalid. Start can equal stop but cannot exceed stop for the search
window. A segment must have strictly increasing bounds. Label is a str
or None; the empty string is a valid visible label. Minimum is an actual
int at least 1. Invalid arguments raise `ValueError("window")`. An invalid
segment or non-Segment item raises `ValueError("segment I")`, with I its
zero-based input position. Validate every segment, even for an empty
window or segments outside it. Do not mutate the input.

Each segment covers its half-open range. Intersect it with the window.
At each tick covered by at least one segment, the segment with greatest
numeric priority wins. If tied, the later segment in iterable order
wins. A winning None is a deliberate blackout and suppresses any
otherwise visible label. Ticks with no coverage are also blank.

Emit contiguous runs of the same visible label, combining adjacent wins
even when the winning segment or its priority changes. Do not combine
across a blank run. After combining visible runs, discard any run shorter
than minimum. Discarded runs leave a gap and never reveal a lower-priority
label. Order the remaining runs by increasing start.

With segments (0, 7, "work", 0), (2, 4, None, 3), and (5, 6, "work", 3),
the window [0, 7) produces `[(0, 2, "work"), (4, 7, "work")]`. A minimum
of 3 drops the first run. The segment at [5, 6) does not split the second.
