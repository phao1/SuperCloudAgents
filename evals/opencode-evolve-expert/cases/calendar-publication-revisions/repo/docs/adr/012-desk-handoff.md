# ADR 012 — Calendar subscriber handoff

        'calendar sync FIRST LAST --at UTC' is the stateful sibling of
        'calendar export'. Feed events keep the legacy UID, escape/folding
        rules and timestamp syntax. Add SEQUENCE starting at 0 for a never
        published shift. Increase by exactly one when a published shift's
        UTC start, UTC end, summary or cancellation status changes. A new
        DTSTAMP, edits to other shifts and changes invisible in the calendar
        do not affect its sequence.

        For a previously published shift that is no longer scheduled, emit
        STATUS:CANCELLED with its last published start, end and summary.
        Keep emitting that tombstone at the same sequence until restored;
        restoration uses the same UID and the next sequence even if it has
        the same hours and title as before. Never invent a tombstone for a
        shift that has not been published. Include both endpoints of the
        requested nominal date range. Live events follow the roster order,
        followed by cancellations ordered by nominal date and UID.

        Retain publication state across CLI calls in an atomic sidecar beside
        the desk, named by appending '.calendar.json' to the desk filename.
        Scope changes to the requested nominal date window. A narrow publish
        must not forget entries outside it. Do not alter the desk or modify
        the stateless export path. Render the complete output before saving
        the revised manifest; file errors use the usual command error exit.
