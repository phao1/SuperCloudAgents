# ADR 006 Planned names and failures

Follow an alias until reaching a canonical package or a name with no
alias. A canonical name takes precedence even if also present as an alias.
Use the first repeated alias in the current resolution chain to identify
an alias loop, and raise `ValueError("alias cycle: a -> b -> a")` for a loop
from a through b, omitting any prefix not in the loop. This error also
applies to optional edges.

A missing required name, whether an explicit target or a nonoptional
dependency, raises `ValueError("missing: NAME")`, with NAME the terminal
name after resolving aliases. If a missing name was optional, add one
skip entry per (canonical parent, terminal name) in order of first
encounter. Other errors reachable from present optional dependencies
still fail the build.

If a dependency visits a canonical package already on the current path,
raise `ValueError("dependency cycle: a -> b -> a")` for a loop from a through
b without the unrelated path prefix. Use canonical names in the cycle.
An already emitted package is never walked again. Evaluate failures in
the same depth-first order used to build the result.
