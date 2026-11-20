# Sample coverage

The import service receives samples from independent stations in arrival
order. Each sample is a station string, integer tick, and an integer value
or None. A later input for the same station at the same tick replaces the
previous input, including if the latest value is None. Stations can take
readings simultaneously. Ticks can be negative. Station strings are compared
exactly, including case and whitespace.

The importer and the dashboard share the history API in `history.py`. Its
result is ordered by tick ascending, with ties in the arrival order of the
winning inputs. Keep the actual winning Sample objects, including clearing
samples, and consume an iterable only once.

Between samples, a station holds the value of its most recent sample. A
None sample clears it until a later non-None sample. Before the first
sample there is no coverage. For a half-open integer window [start, end),
`window.measure` returns a pair (covered ticks, sum of value × duration).
All report calls supply start <= end. A sample at exactly end has no
effect. None and zero mean different things. An empty or uncovered window
returns (0, 0).

`dashboard.panel` formats valid results as `STATION=WEIGHTED/COVERED` using
integers. It raises `RuntimeError("no station coverage")` only for a window
with zero coverage.
