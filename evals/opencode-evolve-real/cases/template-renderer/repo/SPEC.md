# Pocket templates

Pocket renders labels and short status reports from JSON-like data. The only
public entry point is `app.render(template, data) -> str`. The input template is a
Python string, and data is a dictionary with string keys. Nested values are
dictionaries with string keys, lists, strings, integers, booleans, or None.
There are no cyclic values. Never modify the input. Use only the Python
standard library and work offline. Keep this specification and `verify.py`
byte-for-byte unchanged. Run `python -B verify.py` before finishing.

## The source stream

1. Copy literal characters exactly, including CR, LF, spaces, and Unicode.
2. At a run starting with four opening braces, consume the first four and emit
   literal `{{`. Resume scanning immediately after those four characters.
   In any other position starting with `{{`, read through the first following
   `}}`. That entire region is a tag. If the closing braces are absent, the
   parser reports `unclosed-tag` at the opening braces.
3. Trim only spaces and tabs from the ends of the contents of a tag. An empty
   tag and a tag containing CR or LF both report `syntax`. A comment whose
   trimmed contents start with `!` emits nothing. A comment ends at the same
   closing delimiter as every other tag. Closing braces in ordinary text are
   literal.
4. All names are ASCII `[A-Za-z_][A-Za-z0-9_]*`. A path is one or more names
   separated by literal dots, with no intervening whitespace. Numeric indexes
   and arbitrary expressions are outside the grammar. Directive boundaries
   use one or more spaces or tabs. Paths are case-sensitive.

## Tags and nesting

| Contents | Meaning |
| --- | --- |
| `path` | Render the value, per the conversion rules below |
| `! comment` | Emit nothing |
| `#if path` | Open a conditional |
| `#each path as name` | Open an iteration block and bind an alias |
| `:else` | Start the optional alternate body of the innermost open block |
| `/if` or `/each` | Close the matching innermost block |

- An alias may shadow another alias or a root key. The names `root` and `this`
  are reserved and cannot be aliases. Other words only have special meaning
  in the directive slots shown in the table. If any directive has extra tokens
  or uses an invalid path or alias, it reports `syntax`.
- An alternate outside a block, a second alternate in the same block, and a
  close tag with no matching innermost block report `unexpected-block` at that
  tag. If the source ends with blocks open, report `open-block` at the innermost
  remaining opener. Whole-template parsing must finish before rendering starts.
- Empty bodies are valid. There is no implicit whitespace trimming or removal
  of lines containing block tags. In particular, a newline after an opener is
  text in that body. A branch not rendered still has to parse.

## Resolution and evaluation

The first segment `root` means the original data. `this` means the innermost
iteration item, or the original data at the top level. For any other first
segment, resolve the innermost active alias with that name if present, and
otherwise look in the root dictionary. Never fall back through another context
after resolving the first segment. For subsequent segments, look up dictionary
keys only. A path that cannot be resolved is missing. An alias bound to None
still resolves, even if a root key of the same name exists.

- Substitute missing values and None with an empty string. Substitute strings
  unchanged, integers in decimal, and booleans as lowercase `true`/`false`.
  Substituting a list or dictionary causes `not-a-scalar` at the substitution
  tag. Substituted strings are copied once without rescanning.
- Conditionals use Python truth values for the permitted types and treat a
  missing value as false. Only the chosen body is evaluated.
- Iteration evaluates its path in the enclosing context. A missing value, None,
  or an empty list renders the alternate once in that same context. Any other
  non-list value causes `not-a-list` at the opener. A nonempty list renders the
  main body once per item in order, with the alias and `this` referring to that
  item. Restore the enclosing context after every iteration. No separator is
  inserted between renders.
- Stop at the first rendering error in output order.

## Error positions and examples

Raise `app.TemplateError` (a ValueError) with public attributes `code`, `line`,
and `column`. Report the position of the first opening brace of the offending
tag. Both coordinates start at 1, LF advances the line, and every other Unicode
code point advances the column by one. Tabs and CR each count as a single
code point. No exact exception message is required.

For data `{"team":"山","rows":[{"n":2},{"n":0}]}`, the template
`{{#each rows as row}}{{root.team}}={{#if row.n}}{{this.n}}{{:else}}-{{/if}};{{/each}}`
renders `山=2;山=-;`. The literal sequence `{{{{user}}` emits `{{user}}`.
With empty data, `{{#each rows as row}}{{row}}{{:else}}{{this.title}}{{/each}}`
emits an empty string.
