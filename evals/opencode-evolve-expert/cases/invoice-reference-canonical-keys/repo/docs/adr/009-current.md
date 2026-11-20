# ADR 009 — Canonical invoice reference comparison

        Treat an invoice identity as resolved account plus a comparison key.
        Apply the existing reference validation, trim Unicode edge whitespace,
        NFC-normalize, case-fold without locale, then NFC-normalize again.
        Internal spaces and compatibility characters such as full-width
        letters are significant. Never rewrite spelling or spacing in the
        stored record, response or existing statement.

        Use one comparison for issue and find, covering the service API as
        well as the CLI. Duplicate issue in one account uses the existing
        'invoice reference already exists' error. Scoped matches in different
        accounts are independent. A legacy desk can contain two records
        which collapse under the new key. A find with multiple matches uses
        'ambiguous invoice reference', even if one spelling is an exact
        match. Keep both records accessible by numeric ID and allow new
        unrelated references in the same book. No migration rewrites old
        invoices. Failures leave both the file and the invoice ID untouched.
