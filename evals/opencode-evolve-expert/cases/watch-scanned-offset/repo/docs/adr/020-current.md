# ADR 020 — Progress through a filtered watch

        A watch budget applies to **raw** journal records after the supplied
        sequence, before route includes and excludes. The returned after
        position is the last scanned sequence (or the caller's position if
        none was available), even if the last raw record was invisible.
        Return zero visible rows when appropriate and do not scan further to
        fill one page with matches. Respect insertion order, not source stamp.

        'watch NAME --status' adds a boolean 'more' to the regular JSON
        payload. It says whether anything remains unscanned in the raw
        journal at the time of this read. It does not require another match.
        When exactly the budget remained, 'more' is false. Compute it with
        the current read, using at most one raw row beyond the page. Do not
        make a second head lookup with potentially newer data. The regular
        CLI and Watch.read still return only rows and after, with identical
        progress rules on both paths. Empty pages at or beyond the end report
        more=false without changing the supplied position.
