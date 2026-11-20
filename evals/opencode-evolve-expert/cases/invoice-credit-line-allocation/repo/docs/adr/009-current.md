# ADR 009 — Partial invoice credits

        'invoice credit ID DAY AMOUNT REFERENCE' posts a positive amount
        in the invoice's native currency against the outstanding original
        line value after all previously posted credits. Reject a nonpositive
        amount ('credit must be positive'), one above the available amount
        ('credit exceeds remaining invoice amount'), or a date before issue
        ('credit precedes invoice'). Use the original precision and day
        rules. Payment activity is independent of the cap. IDs for credit
        notes form their own insertion sequence. A failed operation consumes
        no ID. Old ledger files still open.

        Apportion the note's minor units in proportion to remaining line
        values. Take whole minor units first. Give leftover units to the
        largest fractional remainders, ties by original line position.
        Already depleted or zero-value lines receive nothing. Emit
        positive shares as {'line': one-based index, 'amount': fixed decimal
        string}, ordered by original position. Do not change the invoice
        or its lines. Return id, invoice, day, reference, amount and lines.
        'invoice credits ID' lists these notes in posting order.

        The statement includes each credit as a negative entry on the
        credit's own posting day, between invoices and payments on that day.
        Use the regular statement FX path for its aggregate signed amount.
        For 'statement aging', reduce each invoice's value by its credits
        dated at or before the report day, then allocate deposits on the
        reduced totals. Reject and roll back the note on a write failure.
