# ADR 009 — Bank receipt identities

        The bank import accepts legacy rows unchanged. A newer row has both
        bank and receipt in addition to the four old fields. Bank is a normal
        normalized ASCII code. Receipt is case-sensitive ASCII, starting
        with a letter/digit followed by up to 63 letters, digits, underscores,
        dots, colons or hyphens. If either provided value is invalid, use
        'invalid bank receipt'. A row carrying only one of the fields or
        unknown fields is an 'invalid remittance rows' error.

        Match by (bank, receipt), across files, multiple rows in one file,
        and past process restarts. Return the original Payment for a matching
        retry, in the same position as that input row. Compare all four
        normalized posting fields; reference preserves literal Unicode and
        whitespace. A mismatched retry raises 'bank receipt already used'.
        Other banks can use the same receipt. Legacy rows are always fresh
        deposits, even when their text or amounts repeat. Never consume a
        local payment ID for a retry.

        Commit payment rows and receipt identities in the same snapshot.
        Any error leaves the ledger and all new receipts from the batch
        unapplied. Old desk files without receipt identities still open.
        Keep the Payment shape and manual posting or archive API intact.
