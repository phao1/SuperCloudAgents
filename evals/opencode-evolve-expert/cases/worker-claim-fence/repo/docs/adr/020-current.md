# ADR 020 — Leases belong to one claim

        A successful claim generates a new opaque identity. A live lease is
        scoped to its Worklist instance, event sequence and owner. It
        remains that claim across extensions, so a handle from an earlier
        extension of the same claim is valid until the current expiry.
        Renewal sets expiry from the current clock and the supplied TTL.

        A past claim can never be used to ack, release or extend replacement
        work, including reuse of the same owner or a release/reclaim pair on
        the same clock tick with identical TTL. Handles from another live
        Worklist instance are also stale, even for the same event. All three
        operations raise RelayError('stale lease') without changing the
        current delivery when given such a handle. Keep the clock injection,
        oldest-first claims, and worklist thread safety. Claim identity
        is opaque to callers and needs no persistent storage.
