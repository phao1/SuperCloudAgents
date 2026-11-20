# ADR 012 — Substitution sheet

        'roster substitute PATH' reads a JSON list with exactly shift, from
        and to string fields on each object. Invalid shapes use 'invalid
        substitution file'. The output is the same list of transfers in input
        order. An empty batch succeeds without a write.

        Interpret all transfers against the original snapshot. Each source
        must be assigned to a scheduled shift. Validate destination members
        and roles just like assignment. An already assigned destination is
        allowed only if that same assignment is also being replaced in the
        batch. A source assignment and destination assignment each appear
        at most once (otherwise 'duplicate substitution source' or
        'duplicate substitution destination' respectively). No-op transfers
        are legal for a currently eligible and assigned member. Transfers
        can swap or chain and cannot cascade. Conflicts with a retained
        assignment use 'already assigned'.

        Replace the assignment and carry its break records, including
        duplicate relief records, to the new member on the same shift.
        Other breaks and assignments are unchanged. Never persist a
        partial batch. The report should show the new crew and their relief
        after reopening the desk without any change to the shift rules.
