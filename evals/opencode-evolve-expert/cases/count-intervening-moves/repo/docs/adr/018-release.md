# ADR 018 — Closing a live physical count

        Accepted. This tightens the quantity comparison in the original guide
        for sheets opened by the upgraded application. Their opening snapshot
        includes an insertion watermark for the movement journal. A close
        rejects any movement after that watermark for a lot **on the sheet**,
        even if its quantity returned to the expected value. Use movement
        insertion order. Business dates can be backdated, duplicated or future
        dated. Lots omitted from the original sheet and journal metadata such
        as notes do not invalidate the count.

        Keep the observation completeness check first, the old direct quantity
        comparison as a fallback for imports/corrections outside the journal,
        and the message 'stock changed during count'. Conflict leaves all
        observations, lot balances and the journal intact. Abort ignores
        conflicts. Both open and close capture/check under their write
        transactions. An already-open v1 sheet has no comparable watermark:
        close it using the old quantity comparison. Preserve its ID, notes
        and observations on upgrade and keep the command response shape.
