# ADR 009 — Payment archive v2 cutover

        Retain the object with integer version and payments array. Add
        'archive export PATH --version 2' and keep the CLI/default 'dump'
        version 1 for existing consumers. Each v2 payment has exactly
        account, day, reference, currency and amount. Account IDs are
        normalized as usual; currency must be a canonical uppercase code
        and match the destination account. The amount is the canonical
        fixed-scale string for that currency, including its trailing zeroes
        and sign. Do not round or silently canonicalize an incoming amount.
        No floating JSON numbers, exponent syntax, redundant zeros or
        negative zero. Usual account, date, reference and nonzero rules apply.

        Both versions are accepted by 'archive import'. Version 1 means
        signed integer cents and can only represent a two-place account.
        Importing or exporting v1 with other account scales fails with
        'archive v1 requires two-place currency'. An invalid v2 payment
        shape, unknown/lowercase currency or noncanonical amount uses
        'invalid archive payment'; a valid but different currency uses
        'archive currency mismatch'. Invalid top-level version/shape uses
        the original error. Resolve all selected rows before writing. Keep
        insertion order, original references, old ledger files and fresh
        destination IDs. Failure leaves the ledger or previous export intact.
