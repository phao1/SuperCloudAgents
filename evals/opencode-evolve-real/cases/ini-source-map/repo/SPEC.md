# INI source-map parser

Release tooling needs a source location for every field in a configuration
document. Implement `app.parse(text: str) -> dict` in standard-library Python.
This document is the complete dialect. The input is a Unicode string. Keep
`SPEC.md` and `verify.py` byte-for-byte unchanged, and run `python -B verify.py`
before finishing.

## Lines and result

Physical lines are delimited by LF or CRLF, and the last line may omit the
delimiter. Strip the delimiter. A remaining CR anywhere on a line is an error
`bare-cr` at that character, even inside a comment. Validate this before
interpreting that line. Space and tab are the only horizontal whitespace
characters, hereafter called padding. All indices in the result and errors
are one-based Unicode code point counts. Tab is one code point.

Return a dictionary of sections in order of first declaration. There is always
a global section with key `""`, placed first. Each section is a dictionary
mapping a key to a three-tuple `(value, line, column)`. The location in the
tuple is that of the first character of the key, regardless of the value
format or number of physical lines. Retain key insertion order. Both keys and
section names are case-sensitive ASCII identifiers starting with a letter or
underscore, followed by any number of letters, digits, underscores, and hyphens.

## Declarations

- Skip a blank line or one whose first non-padding character is `#` or `;`.
- When the first non-padding character is `[`, a section declaration must
  start there. Read up to the first `]`, strip padding from the name, then
  require a valid identifier. If the bracket is missing or the name is invalid,
  report `section` at the opening bracket. After the bracket, skip padding.
  The next character, if any, must begin a `#` or `;` comment. Otherwise
  report `trailing` at that character. Reopening a section makes it active
  without clearing it or changing section order.
- Otherwise split on the first `=`. If it is missing report `assignment` at
  the first non-padding character. The trimmed left side must be a key,
  otherwise report `key` at the first non-padding character (the equals sign
  itself for an empty key). If this key is already present in the active section,
  report `duplicate` at the start of the key before parsing the value.

## Values

After `=`, skip padding. If the first remaining character is `"`, use the
quoted form. Otherwise read a bare fragment, including an empty fragment.

**Quoted.** Start after the opening quote and read until an unescaped double
quote on the same physical line. Interpret `\\` as a single backslash, `\"`
as a quote, `\n` as LF, and `\t` as a tab. Any other escape or a backslash as
the final character of a quoted line reports `escape` at the backslash. An
unclosed quote with no failing escape reports `quote` at the opening quote.
Everything inside the quotes, including spaces, tabs, equals signs and comment
characters, is data. After the closing quote apply the same trailing rule
as for a closing section bracket. A quoted value cannot be continued.

**Bare.** End the fragment at the first `#` or `;` that is either the first
character of the fragment or immediately preceded by space or tab. Strip
trailing padding from the portion before the comment. Ignore leading padding
on each physical line contributing a fragment. If the resulting fragment
ends with an odd-length run of backslashes, remove exactly the final
backslash and continue on the next physical line. The next physical line must
start with space or tab and contain a non-padding character after its indentation.
Otherwise report `continuation` at the consumed backslash. A valid indented
line contributes its own bare fragment even if it looks like a section, key,
quoted value or comment. A comment-only indented continuation contributes an
empty fragment. Join fragments using one LF per continuation. Preserve all
remaining backslashes as literal data. A comment is removed before checking
for the final backslash.

## Errors and examples

Raise `app.IniError` (a ValueError) with `code`, `line`, and `column`
attributes with the specified values. Message text may vary. Stop at the
first error by physical parse order. When waiting for a continuation,
validate any CR on the next physical line before checking indentation.

The source `[ Ops ]\r\n\tname = "亮;い" ; comment\r\n` gives
`{"": {}, "Ops": {"name": ("亮;い", 2, 2)}}`.
For `v=one\` followed by an indented ` [two]`, the global value of `v`
is `"one\n[two]"` with location (1, 1).
For `v=path#fragment # comment`, the value is `"path#fragment"`.
