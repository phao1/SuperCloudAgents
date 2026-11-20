# Capped invoice allocation

Implement `app.allocate(amount, lines) -> dict[str, int]` for distributing a
credit in whole cents among invoice lines. Inputs are integers, so there is
no use of a locale, decimal currency conversion, or current time.

Amount is a nonnegative actual int. Lines is a list of `app.Line` values,
with fields `code` (nonempty string), `weight` (nonnegative actual int),
and `cap` (nonnegative actual int). Codes are compared exactly and must
be distinct, including from ineligible codes. Booleans are invalid as ints.
Validate all input even when amount is zero or all lines are ineligible.
An invalid input raises `ValueError("allocation")`. Never modify the list
or its entries. Return all codes, in input order, with the allocation as
an actual integer number of cents. A line with weight or cap zero gets
zero cents.

Allocate at most the sum of eligible caps. For a nonzero amount left
and at least one eligible line left, compute each remaining line's ideal
share as the amount left times that line's weight divided by the sum of
the weights of remaining lines. Use the exact rational value.

If any ideal share is at or above its cap, settle every such cap using
the shares calculated in that round. Subtract their cents from the
amount left and remove those lines. Recompute using only still eligible
lines. Continue until none are capped or nothing is left.

Allocate the floor of each ideal share in that last round. Give each
remaining cent to a different line, in descending order of fractional
remainder. Break ties by earlier input position. The cap is not shared
with other lines, and any excess when all eligible caps are exhausted
is unallocated. Integer arithmetic must remain exact even above 2^53.

For example, allocate 14 cents to equal weights with caps (3, 10, 10).
The first line gets 3. The remaining shares are 5.5 and 5.5 so the
second line receives 6 and the third 5. With amount 60 the result is
(3, 10, 10), with the rest unallocated.
