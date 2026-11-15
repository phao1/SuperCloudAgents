"""Composition root for the built-in scenarios. API, worker and orchestrator call `load()`."""

from __future__ import annotations

_loaded = False


def load() -> None:
    global _loaded
    if _loaded:
        return
    from sca.kernel.kind import register
    from sca.kinds.analysis.kind import AnalysisKind
    from sca.kinds.coding.kind import CodingKind

    register(CodingKind())
    register(AnalysisKind())
    _loaded = True
