# Workspace manifest filter

`app.included(entries, rules) -> list[str]` filters a manifest of relative
paths. Each Entry holds a case-sensitive path string and a bool `directory`.
Paths use forward slashes between nonempty components, with no leading or
trailing slash and no `.` or `..` components. Input entries can be in any
order. A strict ancestor is always a directory, even when that ancestor
has no explicit entry. Return each surviving entry's original path in its
input order, including repetitions. Do not access the filesystem.

Rules are strings such as `"/cache/"` or `"!src/keep.*"`. Ignore an empty rule
or one whose first character is an unescaped `#`. An initial unescaped `!`
means inclusion, otherwise the rule means exclusion. Process an optional
leading slash (anchored at the workspace root), then an optional trailing
slash (only eligible to match directories). The remaining components are
nonempty and separated by slash. A rule with multiple components is anchored
even without a leading slash. An unanchored single-component rule compares
against the last component at each node examined. There is no whitespace
trimming. The input rules follow the grammar below.

In ordinary components, a literal is exact, `?` matches exactly one Unicode
code point, and `*` matches zero or more code points. Neither matches a
separator. A backslash escapes the next character, which can be `!`, `#`,
backslash, `?`, `*` or ASCII space. Brackets are literal. A component that
consists exactly of two unescaped stars is special. It consumes zero or more
whole path components. An unanchored `**` matches every node. A pattern of
`a/**` also matches directory a itself (the special component may consume
zero levels). Intermediate directory-only constraints come from the path
types and the trailing slash. Wildcards have no behavior specific to dots.

An entry is included only when every strict ancestor is included as a
directory and the entry itself is included with its declared type. Decide
inclusion for each such node by taking the last matching rule for the
exact node. The default is inclusion. A child inclusion rule cannot undo
an ancestor exclusion. Reopening an ancestor requires a rule that matches
that ancestor; earlier exclusion rules may still apply to children.
Ancestors do not need to appear as explicit entries to enforce this behavior.

For example, `build/` followed by `!build/keep.txt` still hides
`build/keep.txt`. Appending `!build/` reopens the directory and the file
survives. A rule `docs/*.tmp` matches `docs/a.tmp` but not
`docs/nested/a.tmp`; `docs/**/*.tmp` matches both.
