"""The hard benchmark keeps equal category coverage across both splits."""

from collections import Counter
from pathlib import Path

from sca.eval.suites import load_suite
from sca.kinds.coding.spec import CodingSpec


def test_evolve_hard_suite() -> None:
    suite = load_suite(Path('evals/opencode-evolve-hard'))
    cases = suite['cases']
    assert len(cases) == 24
    assert Counter(case['split'] for case in cases) == {'dev': 16, 'holdout': 8}
    categories = Counter(case['category'] for case in cases)
    assert len(categories) == 8
    assert set(categories.values()) == {3}
    for category in categories:
        assert Counter(case['split'] for case in cases if case['category'] == category) == {
            'dev': 2, 'holdout': 1,
        }
    for case in cases:
        spec = CodingSpec(repo_url='/fixture', **suite['defaults'], **case['spec'])
        assert spec.image == 'python:3.12'
        assert spec.max_fix_attempts == 1
        assert spec.turn_timeout_s == 420
        assert spec.memory_enabled is False
        assert (Path(case['path']) / 'repo/verify.py').is_file()
        assert case['acceptance_command'] == 'python -B hidden.py'
