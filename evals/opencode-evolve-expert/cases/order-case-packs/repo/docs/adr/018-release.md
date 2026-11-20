# ADR 018 — Dispatch case-pack pilot

        'order pack PATH [--warehouse CODE ...]' reads a JSON array of
        objects with exactly sku, packs and size. Both numbers are positive
        whole integers (booleans are invalid), and their product fits a
        signed SQLite integer. Report 'invalid pack order row' for a wrong
        shape and 'invalid quantity' for bad quantities. Use the catalog
        checks and the warehouse preference rules from the command guide.
        In the service, load_pack_requests returns PackDemand values and
        Fulfillment.pack takes an iterable of them and optional warehouses.

        Plan lines in input order. A pack must come wholly from one warehouse.
        Units for packs at that warehouse can span lots and follow FIFO across
        lot age and ID. Consume as many complete packs as possible at each
        preferred warehouse in order. Subtract those picks from availability
        for later lines, keeping the leftovers available at their original
        sites. Repeated SKU lines are independent demands, even if pack sizes
        match. All planning is read-only. This is a priority preview; do not
        reassign earlier lines to make a later line more successful.

        Return {'picks': ..., 'shortages': ...}. Picks use the normal
        sku/warehouse/lot/lot_id/quantity projection plus a 1-based 'line'.
        Quantities are units. Emit one pick per line and contributing lot,
        ordered by line, warehouse preference and FIFO. Shortages carry 'line',
        'sku' and 'missing_packs' for the unsatisfied lines in input order.
        No per-pack expansion is needed: work in aggregate lot quantities.
