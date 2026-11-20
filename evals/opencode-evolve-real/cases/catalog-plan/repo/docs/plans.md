# Build plans

`app.build(targets, records, aliases)` returns a JSON-ready dict for a local
component build. Inputs consist of a list of target name strings, a dict of
canonical name to `graph.Package`, and a dict of alias to name. Names are
lowercase ASCII letters, digits and hyphens and start with a letter. All input
shapes and names are valid. Package versions are strings and may be empty.

Process targets in their supplied order. Visit each package's dependency
list in its supplied order. Emit a package after all of its dependencies.
Emit each canonical package at most once. Record who first selected it as
described in output.py. A later explicit target does not replace that
information. Never mutate the catalog or the dependency lists.

All targets and dependencies pass through alias resolution. The details,
including skipped optional edges and error messages, are in
[ADR 006](adr/006-aliases.md). The docstrings in graph.py and output.py
describe the package and result structures. No pre-validation of unused
entries or aliases is required.
