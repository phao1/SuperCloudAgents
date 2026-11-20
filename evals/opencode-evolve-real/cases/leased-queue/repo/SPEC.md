# Leased work queue

Implement `app.Queue(clock, max_attempts)` to dispatch work to one or more
callers using a single in-memory queue. Public calls are synchronous. The
fixture only invokes one call at a time. Use the standard library, offline,
and the injected clock as the sole time source. Preserve the bytes of this
spec and `verify.py`. Run `python -B verify.py` before finishing.

## Arguments and clock

The constructor takes a callable clock with no arguments and a positive actual
int for max_attempts. It never reads the clock. Invalid configuration raises
`ValueError("configuration")`. An ID is a nonempty case-sensitive str. A
payload is a str, including empty. A priority is any actual int. Delay is
an actual int at least 0, and a lease, limit or attempt argument is a
positive actual int. Booleans are invalid as integers. Reject invalid method
arguments with `ValueError("arguments")` before any time read or state change.

Otherwise read the clock exactly once per call, except replaying submit
as described below. A tick is a nonnegative actual int at least as large as
the last successfully observed tick of this instance. An invalid value
raises `ValueError("clock")`. If the clock raises, propagate that exception
unchanged. Invalid or failed reads change nothing.

At the start of each operation with a valid clock reading, expire every
lease whose deadline is at or before this tick. A job whose last attempt
has reached the max is now dead. Any other expired job goes back to pending
with eligibility at its **lease deadline**, even if the next observation
came much later. All jobs remain recorded, including terminal states.
Each instance has its own jobs.

## Submitting and reserving

`submit(job_id, payload, *, priority=0, delay=0) -> bool` creates a pending
job with attempts 0 and eligibility at sampled time plus delay. Return
True. The first successful submit fixes the insertion order and the
original payload, priority and relative delay. After argument validation,
an exact replay of those original parameters returns False with no clock
read, even for a terminal job. Reusing the same ID with any differing
parameter raises `ValueError("conflict")` before sampling the clock.

`reserve(limit, lease) -> list[tuple]` leases up to limit ready jobs
(pending with eligibility time no later than the tick). Choose the
largest priority first. Within a priority, select by earliest eligibility
time and then original submit order. Every job selected goes to leased,
increments its attempt number and receives a deadline at sampled time
plus lease. Return tuples `(job_id, payload, attempt, deadline)` in
selection order in a fresh list. A job cannot be reserved twice in one call.
An empty selection returns an empty list.

## Acknowledging and retrying

`ack(job_id, attempt) -> bool` succeeds only for a currently leased job
whose attempt number matches the argument. It marks it done, clears its
deadline, and returns True. Otherwise return False, including on dead,
unknown, pending, or completed jobs. At equality with the deadline the
lease is already gone.

`retry(job_id, attempt, *, delay=0) -> bool` has the same lease-match
rules. On success it clears the lease. It transitions to dead if max
attempts have already been reached, otherwise to pending with eligibility
at sampled time plus delay. Return True for either successful transition,
including a transition to dead. Both operations perform the expiry sweep
even if the ID or attempt number does not match. A stale attempt can never
complete or retry a later lease of the same job.

## Inspecting work

`snapshot() -> dict` takes a time observation and returns a new dictionary
in original submission order. A job maps to a tuple `(state, attempts, at)`.
State is `"pending"`, `"leased"`, `"done"`, or `"dead"`. At is the
eligibility tick for pending jobs (even when ready), deadline for leased
jobs, and None for terminal jobs. Mutating the dictionary has no effect.

For example, submit and reserve job x at tick 4 with lease 2. Its lease
deadline is 6. If the next observation is at tick 9, pending x has
eligibility at 6 if it still has attempts left. After re-reserving x, an
acknowledgment from the first attempt returns False.
