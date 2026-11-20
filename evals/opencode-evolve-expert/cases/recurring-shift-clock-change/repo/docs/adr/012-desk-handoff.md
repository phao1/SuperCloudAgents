# ADR 012 — Resolving venue clock changes

        A recurring rule or a replacement wall time names a civil minute at
        its venue. If the reading occurs twice on that date, use the later
        absolute instant. If the reading does not occur, use the first
        representable local minute at or after it, which may be at a half
        hour transition rather than an hour boundary. Resolve both cases
        by comparing absolute times after projecting the candidate back to
        venue time. An ordinary minute retains its exact local value.
        Maintain shift identity, nominal date, skips and elapsed duration.

        Use the same absolute start for roster list, direct lookup,
        assignments, calendar export and the coverage board. Ordering
        remains by actual instant, lane and ID. Local policy belongs in the
        shared schedule conversion so operators get consistent results even
        when changing venue or applying a one-off start. The clock holds at
        minute granularity for this service; do not add a fixed hour.
