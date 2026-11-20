# ADR 018 — CSV receiving diagnostics

        Applies to 'file import-receipts' and the file service. Keep the base
        guide's columns, optional values, UTF-8, arbitrary header order and
        all-or-nothing writes. Accept a UTF-8 BOM at the start of the file.
        Quoted text keeps its embedded newlines and separators verbatim.
        A blank physical row outside a quoted field is ignored. Report physical
        lines starting at 1, including the header and blank rows. The line of a
        record is where it begins, even when CSV parsing fails after several
        lines or a memo continues on the next line.

        Header/column errors still use 'invalid receipt columns'. A malformed
        data row (strict quoting errors, field-count mismatch, bad integer)
        uses 'invalid receipt line N'. An unreadable file or invalid UTF-8
        uses 'invalid receipt file'. Parse all CSV syntax before receiving any
        records. Then validate and apply each receipt in file order in a single
        transaction. A validation or stock conflict from that phase keeps its
        exception class and reason, prefixed by 'receipt line N: '. Keep the
        results and normal journal unchanged on success. Keep calls to
        csv_requests(lines) returning ordinary Intake objects and leave direct
        Inventory.receive_all callers' error messages unchanged.
