# UTC cron calendar

The scheduler expands recurring minute labels for a reporting service. The
authoritative rules are in this document. The module `app` exposes a
`Cron(expression: str)` constructor, `Cron.matches(when)`, and
`Cron.between(start, stop)`. Use only the standard library, offline. Neither
the current time nor the machine's locale or time zone is an input. Keep
`SPEC.md` and `verify.py` byte-for-byte unchanged. Run `python -B verify.py`.

## Five fields

The expression has exactly five nonempty fields separated by one or more ASCII
spaces or tabs. Spaces and tabs before or after the expression are permitted.
All other whitespace is an ordinary invalid character. Field indexes for
diagnostics are zero-based.

| Index | Unit | Allowed | Aliases |
| --- | --- | --- | --- |
| 0 | Minute | 0–59 | none |
| 1 | Hour | 0–23 | none |
| 2 | Day of month | 1–31 | none |
| 3 | Month | 1–12 | JAN, FEB, MAR, APR, MAY, JUN, JUL, AUG, SEP, OCT, NOV, DEC |
| 4 | Day of week | 0–7 | SUN, MON, TUE, WED, THU, FRI, SAT |

Aliases use ASCII letters and are case-insensitive. SUN is 0, SAT is 6, and
7 is another spelling of Sunday. A decimal term is one or more ASCII digits.
Leading zeroes are allowed. Decimal components represent their mathematical
integer value. All numeric components in valid test inputs fit in 9 digits.
Malformed expressions may include larger values.

## Expanding a field

- A field is a comma-separated list of one or more terms. Process all terms,
  including ones that appear after an already full range. Empty terms fail.
- The base of a term is `*`, a single value, or two values separated by `-`.
  The star stands for the field's complete range from low to high. The end of
  a range must be at least its beginning; there is no wraparound. A value may
  be an alias where allowed. Bounds are inclusive.
- A single optional slash followed by an ASCII decimal step can suffix any
  base. A step is 1 through the size of the numeric field, inclusive. With a
  step, a single value is the lower bound of a range ending at the field's
  maximum. Without a step, a single value expands to itself. Select the lower
  bound first and increment by the step, stopping before exceeding the upper
  bound. For the weekday field, convert 7 to 0 after the expansion, including
  after stepping. Repeated selected values never create duplicate schedules.
- Characters other than those used above, open-ended ranges, signed values,
  descending ranges, and invalid values or steps cause an error. Report
  `app.CronError` (a ValueError) with public `field` holding the first invalid
  field's index. A non-string expression or the wrong number of fields raises
  the same exception with `field == -1`. The exception message is free.

## Matching dates and times

Every time argument must be a datetime whose `tzinfo is datetime.timezone.utc`
and whose year is between 1970 and 2099 inclusive. Invalid arguments raise
`ValueError("utc datetime required")`. No conversion of other zones is required.
Calendar legality follows the datetime calendar. Matching uses minute, hour,
and month as successive required filters. Seconds and microseconds must both
be zero to match.

A day field is unrestricted only if its source text is exactly `*`. If both
day fields are unrestricted, every calendar day passes the day check. If only
one is unrestricted, the other field controls the check. If both are restricted,
the day check passes when either field selects the date. The syntactic rule
means `*/1` and a complete numeric list still count as restricted. For Gregorian
weekday numbers, Monday is 1 and Sunday is 0.

## Range results

`between` returns a list of distinct UTC datetimes in ascending order for
`start <= occurrence < stop`. Validate both endpoints first. If stop precedes
start or the interval exceeds 32 days, raise `ValueError("invalid window")`.
Equal endpoints give an empty list. Include an exact matching start; if start
has nonzero seconds or microseconds, the first candidate is the following minute.
All results have zero seconds and microseconds. Correct work on empty ranges
and ranges containing no matches is part of the interface.

## Examples

- `30 8 * JAN MON-FRI` selects 08:30 on January weekdays.
- `0 0 31 * MON` selects midnight on Mondays and on dates numbered 31.
- `0 0 * * 1-7/3` selects Monday, Thursday, and Sunday. The raw expansion
  is 1, 4, 7.
- `0 0 */1 * MON` selects midnight every day. The numeric expansion of
  its restricted day-of-month field happens to match every calendar day.
