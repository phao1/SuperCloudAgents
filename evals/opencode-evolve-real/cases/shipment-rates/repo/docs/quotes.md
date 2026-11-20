# Parcel quotes

`service.quote(postcode, parcels)` returns a dict with keys `zone`, `lines`,
`discount_cents`, and `total_cents`. Line order is parcel input order and each
line has `shipping_cents` and `handling_cents`. Empty parcels are permitted
and yield no lines and zero discount and total.

Address rules live in ADR 004. Invalid input to the service raises
`ValueError("postcode")` for invalid addresses or `ValueError("parcels")` for an
invalid list or parcel. Validate postcode first. Keep both inputs unchanged.
Read the parcel docstring in `pricing.py` for accepted fields. The exported
`rate` function also follows the charging rules for one valid parcel.

`python -B -m cli` reads a JSON object from stdin with exactly `postcode` and
`parcels` keys. On success, print the JSON quote with one trailing LF and
exit zero. JSON member order and insignificant whitespace before the LF may
vary. For malformed JSON, the wrong request shape, or any invalid input to
the service, exit 2 with empty stdout and exactly `invalid quote
` on stderr.
