"""Scenarios ("kinds").

Deliberately empty: Temporal's workflow sandbox re-imports a workflow module's packages on every
workflow run, so anything imported here would be reloaded each time (this once cost ~130 MB per
run). Built-in kinds are registered explicitly by `sca.kinds.builtin.load()`.
"""
