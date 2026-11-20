# ADR 018 — Activity feed handoff

        'report activity-page --size N [--cursor TEXT]' accepts the same SKU,
        warehouse, day-window and kind filters as 'report activity'. It prints
        one JSON object with 'rows' (normal Movement values) and 'next' (an
        opaque versioned ASCII token, or null). Size is an integer 1 through
        500, default 100. A terminal or empty page has no token. The original
        command's filters, CSV, ordering, limit and array shape are independent.

        Integrations start scanning when the first command opens its page.
        Resume that view of the append-only movement log, ordered by business
        day and then ID, excluding every subsequently inserted row, including
        backdated rows. Do not compare just the day or just the ID. The token
        travels across CLI calls and process restarts and binds all normalized
        filters, but callers may change page size. Return 'invalid activity
        cursor' for malformed/unknown versions or inconsistent fields or
        filters. Tokens are portable within this depot database; they are
        pagination state rather than authentication credentials.

        Limit the storage query to a page plus one. The cursor's resume point
        and captured journal high-water mark belong in the SQL predicate so
        a large export does not materialize all the rows to serve the page.
Service callers use Reporting.activity_page with the ordinary activity
filters and keyword arguments size and cursor. Its result exposes
the rows and next fields.
