# Board preview

An import supplies an immutable Snapshot with a board slug, nonnegative
revision, and tuple of Item objects. Revision numbers are unique within a
board only. Readers use `Preview.build(snapshot, tag=None, descending=False,
limit=None)` for the bundle and `panel.draw` for the display.

A bundle is a dictionary with exactly one key, the board slug. Its value is a
tuple of display labels. With a tag supplied, include only items whose exact
tag equals it, including when the tag is the empty string. None means all
tags. Sort by label in Python string order, descending if requested. Preserve
input order on ties. Apply the nonnegative limit after sorting. None means
unlimited. Zero gives an empty tuple.

Repeated reads of the same logical query must reuse the cached bundle
without re-running the builder, even if the Snapshot instance differs.
The board slug and revision identify the immutable data, and the exact tag,
ordering and limit identify the query. Distinct combinations must stay
independent, including falsey query options. Two Preview instances use
independent memo tables. There is no requirement to evict old revisions.
The contract of the reusable memo table is in the Memo docstring.

The panel displays the exact bundle for the requested board, with labels
separated by LF and no trailing LF. An empty result displays the empty string.
