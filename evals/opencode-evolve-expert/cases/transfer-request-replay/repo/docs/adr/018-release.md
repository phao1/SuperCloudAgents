# ADR 018 — Move requests from dispatch

        The local operator command is still 'lot move'. Dispatch appends
        '--request-id ID'. The service exposes the same option as the
        keyword-only 'request_id' argument of Inventory.move. Omitted IDs
        retain the legacy behavior. IDs are case-sensitive ASCII letters or
        digits followed by up to 63 letters, digits, underscores or hyphens
        (otherwise 'invalid request id'). Scope is one database.

        A committed ID belongs to a normalized request tuple (sku, source,
        destination, quantity, day, memo). Codes normalize like normal moves.
        Memo is compared exactly, including surrounding spaces. Reusing an ID
        with a different tuple raises Conflict('request id already used').
        A matching retry returns the original ordered list of destination Lot
        snapshots, including quantities and IDs as returned originally. Check
        the receipt before checking availability or catalog active flags, so
        subsequent shipments and operator settings do not change the reply.

        Move movements and the receipt commit together. A failed move leaves
        the ID free to retry. Two writers racing for an ID serialize before
        checking it. Read the original SQLite schema on new installations
        and migrate existing database files incrementally. Old stock history
        and responses must stay readable.
