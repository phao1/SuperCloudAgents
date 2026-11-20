# ADR 020 — Regional route fanout

        A terminal segment '**' matches zero or more complete segments.
        It may be the entire pattern. A single '*' still matches exactly one
        segment. Reject '**' anywhere else and wildcard fragments. A route
        must match an inclusion and no exclusion. Rule matching is case
        sensitive; any exclusion wins within that route, even over a more
        specific inclusion. Keep registration order and emit each name once.
        Apply the grammar and precedence to saved routes, preview, watch and
        dispatch, including old saved definitions and files after reload.

        RoutingTable is a reusable snapshot. Compile its indexing work when
        building it or at first use. Repeated lookups among tens of thousands
        of unrelated routes must avoid scanning the entire route directory
        for every stream. Bounds are a few CPU seconds for a few thousand
        varied lookups in a sparse 20,000-route table. The contract does not
        prescribe a data structure. Saving a new directory entry does not
        change an already constructed RoutingTable.
