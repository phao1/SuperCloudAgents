# ADR 012 — Accounting days at a venue

        The payroll preview includes duty intersecting the selected civil
        dates at the shift's venue, even for duty assigned on an earlier
        nominal date. Use the actual elapsed interval between the venue's
        two successive civil midnights. A clock-change day can have 23 or 25
        hours. The venue date ranges are inclusive; each day interval is
        start-inclusive and end-exclusive. Group by day, venue and member.

        Breaks are offsets from the absolute shift start. Clip them to the
        effective shift (an existing break can extend past a later shorter
        override), subtract the union of those intervals, then credit the
        elapsed paid time within each requested day. An overlapping break
        never gets deducted twice. Count overlapping shifts independently.
        Reports ignore cancelled shifts, omit zero rows, keep stable sorting,
        do not mutate the desk and keep the existing venue filter.
