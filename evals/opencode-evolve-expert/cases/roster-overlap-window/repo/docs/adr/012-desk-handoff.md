# ADR 012 — Dispatch overlap review

        The two review timestamps are absolute bounds of a nonempty half-open
        window. Report every unordered pair of distinct scheduled shifts for
        the requested member whose actual overlap intersects that window.
        Both shifts may have started before the window and can belong to
        different nominal dates or venue zones. Touching endpoints alone
        are not overlap. Output start and end of the intersection of both
        shifts and the review window, in UTC.

        Omit cancelled shifts, other crew and unassigned shifts. Use the
        roster sort order (actual start, lane, identity) to decide first and
        second and order the output rows by their first shift then second.
        Contained shifts can overlap several simultaneous assignments.
        Preserve existing CLI shape and validation. Review never edits work.
