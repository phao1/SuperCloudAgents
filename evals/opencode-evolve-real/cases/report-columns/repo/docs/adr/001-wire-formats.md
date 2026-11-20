# ADR 001 Report exports

A CSV export starts with the display headers, then one row per source record.
It quotes a cell if it contains a comma, a double quote, CR or LF; double
literal quotes within a quoted cell. Also quote a sole empty cell on a row
to distinguish it from a row with zero columns. End every row with LF.
Use an empty record line for a zero-column row (including zero headers).
Write integer cells in decimal. No BOM or other padding is added.
The projection warnings are absent from CSV output.

A JSON export is a single object with keys `headers`, `rows`, `warnings`
in that order. Convert the tuples to JSON arrays. Preserve Unicode literally
and use no whitespace around commas or colons. Include a single LF at the end.
Retain integers as numbers. An empty selection still produces one empty
row array per source row. CSV and JSON produce Python strings.
