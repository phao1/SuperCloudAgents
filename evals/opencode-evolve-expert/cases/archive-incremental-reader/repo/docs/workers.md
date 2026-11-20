# Worklists and watch

Worklists are local and rebuilt from the event journal by the owner.
One Worklist instance can be shared by threads. The owner supplies
an integer clock measured in milliseconds. A positive lease TTL is
added to that clock. When the clock reaches the expiry exactly the
claim is no longer live. Claims pick the earliest enqueued sequence
available for delivery. A completed event is not offered again.

A watch is a bounded read over the underlying journal combined with
the named route. Route rules accept a whole segment '*' for exactly
one segment. A stream must match at least one inclusion and no
exclusion. Case matters. Previewing a route never writes a record.
Route names are ASCII identifiers, in registration order.
