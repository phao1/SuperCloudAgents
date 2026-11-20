# Atomic quota buckets

A local scheduler shares a fixed capacity across multiple independent
resource names. Callers sometimes need more than one resource at once.
Expose `app.Buckets(capacity, refill, period, clock)` and its
`attempt(costs, *, commit=True)` method. Use only the Python standard
library and work offline. The only source of time is the supplied clock.
Keep this document and `verify.py` byte-for-byte unchanged and run
`python -B verify.py`.

## Configuration and arguments

- Capacity, refill and period must be positive actual ints. Booleans
  are invalid. Capacity is the whole-token burst ceiling for each
  individual resource. Refill is the number of tokens added over a
  period of ticks, as an exact continuous rate over integer tick time.
  A refill larger than capacity is accepted. `clock` is a callable
  that takes no arguments. Invalid configuration raises
  `ValueError("configuration")` and construction never calls the clock.
- Costs must be a nonempty dictionary. Its keys are nonempty strings.
  Every cost is an actual int in the inclusive range 1 through capacity.
  `commit` must be an actual bool. Invalid arguments raise
  `ValueError("costs")` and do not read the clock or affect state.
  Neither the dictionary nor any returned balances may give callers
  access to mutable state inside the buckets.

## Accounting

Each valid attempt samples the clock once. A tick is a nonnegative
actual int. The result must be greater than or equal to the preceding
successfully observed tick for the same instance. If validation fails,
raise `ValueError("clock")` with all state as before the call.
Exceptions raised by the clock propagate unchanged, with the same
rollback behavior. Valid but denied attempts and previews still count
as successfully observed ticks.

A resource that has never been charged starts at full capacity. In a
resource that has been charged, add the exact amount
`elapsed * refill / period` since that charge, clamping the balance to
capacity. Keep fractional credit between observations and successful
charges. Time spent full cannot be banked. No resource shares a balance
or observation record with another instance.

The permitted arithmetic is exact, including for integers larger than
the precision of a binary float. An equivalent integer accounting
uses units of 1/period token, adding refill units each tick and
subtracting period units per charged token. The API only exposes whole
units of tokens in balances, rounded down.

## Batch decision

Return `app.Decision(allowed, retry_at, balances)` with public attributes
`allowed: bool`, `retry_at: int | None`, `balances: dict[str, int]`.
Balances include exactly the queried keys in their original order.

1. Replenish the queried resources to the sampled time and compare all
   against their requested costs.
2. If all can pay, return `allowed=True`, `retry_at=None`. With
   `commit=True`, charge every resource and report its remaining balance.
   With `commit=False`, charge nothing and report its pre-charge balance.
3. If any resource cannot pay, report `allowed=False`, charge nothing
   even when `commit=True`, and report all pre-charge balances.
   `retry_at` is the earliest **absolute tick** at which this same batch
   would be affordable if no intervening charges happen. Round the
   wait upward to the next integer tick per insufficient resource,
   and take the largest wait.

A preview is observational apart from advancing clock validation. A
denied attempt cannot reserve credit or delay another attempt. Returning
the state reached by lazy replenishment is equivalent to keeping every
resource replenished after every clock observation.

## Worked example

Capacity 3, refill 2, period 3. At tick 10 charge `{"read":3}` and get
balance 0. At tick 11, `{"read":1,"write":1}` is denied with balances
`{"read":0,"write":3}` and retry tick 12. At tick 12, the same batch
can pay and leaves balances 0 and 2. The read bucket actually retains
1/3 token. At tick 13 it can pay for another single token.
