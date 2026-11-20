# TTL/LRU cache

Implement `app.Cache(capacity, clock, on_evict=None)`. It stores caller-owned
Python values for short-lived reports. This is a synchronous in-memory
component. Use only the standard library and work offline. Keep this
specification and `verify.py` byte-for-byte unchanged. Run
`python -B verify.py` before finishing.

## Configuration and time

Capacity is a positive actual int and clock takes no arguments.
If given, `on_evict` must be callable. Invalid configuration raises
`ValueError("configuration")`. Construction never samples the clock.
A key is a nonempty string, matched exactly. A TTL is None or a
positive actual int (a bool is invalid for capacity and TTL).
An invalid key raises `ValueError("key")` and an invalid TTL raises
`ValueError("ttl")`. Validate a put's key before its TTL. Argument
validation precedes the clock and all cache effects.

Each valid public call samples the clock once, including a miss.
The clock yields a nonnegative actual int. It must stay at or above
the most recently observed tick for this cache. For an invalid or
backward tick raise `ValueError("clock")`, without updating the
last tick. A raised clock exception propagates unchanged. Neither
clock failure changes cache contents or invokes callbacks.

After receiving a valid tick, expire all entries with a finite
deadline at or below that tick. Remove them before any requested
operation. Entries with no TTL have no deadline. Removing expired
entries leaves the relative recency order of the remaining entries
unchanged. Expiration notices are queued by ascending deadline,
with ties in least-recent to most-recent order as at the start of
the call. In particular, a prior get can reorder equal-deadline
expiration notices without extending their deadline.

## Operations and recency

Values may be any Python object, including None. Keep and return the
same object reference. Newly written or accessed entries become the
most recent. Iteration is always over live entries.

| Operation | Result and state change |
| --- | --- |
| `put(key, value, ttl=None)` | Return None. A finite deadline is sampled tick plus TTL. A live entry with the same key is removed and reports replacement before the key is inserted at the most-recent end. Replacement resets the deadline even if the value is identical. If over capacity after insertion, remove the least-recent entry. |
| `get(key, default=None)` | Return the live value and make it most recent. On a miss return the supplied default as is without changing recency. |
| `delete(key)` | Remove the live entry if present and return True. If it was expired or absent, return False. |
| `items()` | Return a new list of `(key, value)` tuples ordered from least to most recent. Do not update recency. Mutating the list has no effect on the cache. |

Only these operations read the clock. Valid operations through the public
API must leave the cache within capacity. Multiple Cache instances have
independent data.

## Notifications

If present, call `on_evict(key, value, reason)` once for every removed
entry. Reason is `expired`, `replaced`, `capacity`, or `deleted` as
appropriate. Send all expiration notices first in the order described
above, then any removal specific to the requested operation. Removal
of an expired key emits only `expired`; later reinserting the key
does not emit `replaced` for the expired entry. Pass the original
value object.

Calculate the result and commit the entire cache operation before
delivering any notices. If a callback raises an ordinary Exception,
continue delivering the remaining notices and finally re-raise the
first exception object. The operation is still committed, and no
notices may be repeated by the next call. Callbacks used here do not
call back into the cache.

## Example

At time 0, insert `a` and then `b` with TTL 2. Read `a` at time 1.
At time 2 a `put("a", new)` first reports expired `b` then expired
`a`. The new `a` has no deadline and there is no replacement notice.
