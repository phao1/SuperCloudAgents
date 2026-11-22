from pathlib import Path

from sca.api import evals
from sca.config import Settings
from sca.core.models import Principal
from sca.eval.suites import load_suite, select_cases
from sca.kinds.coding.spec import CodingSpec

EVALS = Path(__file__).resolve().parents[2] / "evals"
SUITE = EVALS / "dsh-smoke"


def test_dsh_smoke_cases_match_original_bytes_and_load_with_smoke_defaults():
    suite = load_suite(SUITE)
    assert suite["name"] == SUITE.name and suite["kind"] == "coding"
    assert len(suite["cases"]) == 3
    assert {case["id"] for case in select_cases(suite, "dev")} == {"interval-union", "unicode-offsets"}
    assert [case["id"] for case in select_cases(suite, "holdout")] == ["lazy-zip-strict"]
    for case in suite["cases"]:
        new = Path(case["path"])
        original = EVALS / "opencode-evolve" / "cases" / case["id"]
        original_files = {path.relative_to(original) for path in original.rglob("*") if path.is_file()}
        assert {path.relative_to(new) for path in new.rglob("*") if path.is_file()} == original_files
        for relative in original_files:
            assert (new / relative).read_bytes() == (original / relative).read_bytes()
        spec = CodingSpec(repo_url="/fixture", agent_backend="dsh", **suite["defaults"], **case["spec"])
        assert spec.max_fix_attempts == 0 and spec.turn_timeout_s == 240 and not spec.memory_enabled


async def test_dsh_smoke_lists_both_splits_for_the_portal_evolution_form(monkeypatch):
    async def inline(listing):
        return listing()

    settings = Settings(_env_file=None, eval_suites_dir=EVALS)
    monkeypatch.setattr(evals, "get_settings", lambda: settings)
    monkeypatch.setattr(evals.asyncio, "to_thread", inline)
    suites = await evals.suites(Principal(tenant_id="eval", subject="fixture"))
    smoke, = (suite for suite in suites if suite["name"] == "dsh-smoke")
    assert {case["split"] for case in smoke["case_metadata"].values()} == {"dev", "holdout"}
