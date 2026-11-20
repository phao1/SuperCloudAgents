# CSV dialect sampler

Implement `app.records(text, delimiter)` and `app.sniff(text)` for a
spreadsheet import preview. This is a strict, small CSV dialect whose
contract is entirely in this document. Use only the standard library,
offline, without changing `SPEC.md` or `verify.py` byte-for-byte.
Run `python -B verify.py` before finishing.

## Reading candidate records

Input is a Unicode string. Valid delimiters are comma, semicolon, ASCII tab,
and vertical bar, in that preference order. Passing any other delimiter to
`records` raises `ValueError("delimiter")`, before looking at the source.
Remove exactly one U+FEFF if it is the first character. All diagnostic offsets
still count it. A second BOM or a BOM anywhere else is ordinary field data.

- A row has one or more fields. Delimiters end a field and begin the next one.
  The last field can be empty, including after a trailing delimiter. LF or
  CRLF ends a record, and the last record may end at end-of-input. A truly
  empty physical line outside a quoted field is skipped, even at the end.
  Spaces, explicit delimiters, and quoted empty fields are data and make a
  record nonempty. The absence of any rows yields an empty list.
- At the start of a field, a double quote opens a quoted field. Inside quotes,
  a pair of adjacent double quotes represents a single data quote. A quote
  without another quote immediately following closes the field. After a
  closing quote the next character must be a delimiter, a record terminator,
  or end-of-input. Even spaces and tabs are forbidden here unless the tab
  is the delimiter.
- A quote in an unquoted field is invalid. Whitespace is data in unquoted
  fields as well as quoted ones. Inside quotes keep the exact original LF,
  CR, CRLF, delimiters and Unicode. Outside quotes, CR must be followed by
  LF and the pair forms one logical terminator. A bare CR is malformed.
- The return value of `records` is a list of lists of strings, in source
  order, with quote syntax removed. No width consistency is required to
  call `records`.

## Diagnostics

Malformed CSV for the selected delimiter raises `app.CsvError` (a ValueError)
with `code` and `offset`. Offset is original zero-based Unicode code point
position. A quote inside an unquoted field reports `quote` at the quote.
Non-delimiter content after closing quotes reports `trailing` at that
content. Bare CR outside quotes reports `newline` at the CR, including
immediately after a closing quote. At end of input, any still-open field
reports `unclosed` at its opening quote. When there are multiple errors,
report the first error in parsing order. The message may vary.

## Selecting a dialect

`sniff` considers the four delimiters independently. A candidate with a
malformed parse is discarded even if all earlier rows for it were valid.
For a complete successful parse, count the number of records for each
width (number of fields). To qualify, a width must be at least 2, have at
least two records with that width, and account for a strict majority of
all records parsed with that delimiter. Width-one records contribute to
the total even though they cannot be the qualifying width. A candidate
has at most one qualifying width.

Among eligible candidates, prefer most records of the qualifying width;
break ties by larger qualifying width, then by delimiter preference.
Return `None` if no candidate qualifies. Otherwise return
`app.Dialect(delimiter, width, support, newline)`. The first three fields
contain the selected delimiter, qualifying width and number of rows of that
width. `newline` is the most frequent actual terminator among all nonempty
records parsed under the selected dialect, including rows of other widths.
Only outside-quote terminators count. Break ties in favor of LF, also the
default if terminators are absent. The last row without a terminator does
not contribute a vote.

## Examples

- `a;b\r\n"1;2";3\r\n\n` has delimiter semicolon, width 2, support 2,
  and newline CRLF.
- `x,y|z\na,b|c` selects comma. Both comma and pipe have two width-two
  records, so preference order decides.
- `a,b\na,b\na\na` has no dialect. The comma sample has two width-two
  records, exactly half the total.
- `a,"x\r\ny",\n` under comma has one row with three fields including
  an empty trailing field. The only record terminator is LF.
