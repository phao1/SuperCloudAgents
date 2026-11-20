# ADR 020 — Retained journal floor

        The journal keeps an insertion high-water mark when its last remaining
        records are pruned, including after reopening. Append after the
        greatest sequence ever assigned. Prune removes a prefix only and
        records the greatest sequence actually removed, never a supplied
        number beyond the current head. No-op prune changes no stored bytes.
        Keep the original record format. In v2 store a first line
        '{"pruned_through":N}' in the same file once a prefix has been
        removed. N is a positive sequence, strictly below every retained
        sequence. Legacy files, including empty ones, open with floor 0.

        Update the retained records and the header together in one atomic
        replacement. A write failure preserves both the live and on-disk
        state. A corrupt v2 header is an invalid journal file. A read_after
        strictly below the floor raises RelayError('history starts after N')
        so an old explicit watch/export cursor cannot silently skip a gap.
        The exact floor is a valid cursor. Journal.all and CLI listing or
        archive export without an explicit cursor still return retained
        records. Preserve --limit on unpositioned listings.
