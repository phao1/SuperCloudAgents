"""Pure static checks, ranking and paired promotion gates."""

import hashlib
import json
import re
from statistics import mean

from sca.eval.compare import compare
from sca.eval.trajectory import overall_passed
from sca.evolve.models import Overlay


def overlay_hash(overlay: dict) -> str:
    return hashlib.sha256(json.dumps(overlay, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def static_check(overlay: dict, texts: list[str], surfaces: list[str], parent: dict | None = None) -> dict:
    reasons = []
    try:
        Overlay.model_validate(overlay)
    except ValueError:
        return {'valid': False, 'reasons': ['invalid overlay']}
    parent = parent or {}
    if any(overlay.get(key) != parent.get(key) for key in overlay.keys() | parent.keys() if key not in surfaces):
        reasons.append('surface is not permitted')
    prompt = overlay.get('system_prompt')
    if prompt is not None and '${OPENCODE_TOOL_GUIDANCE}' not in prompt:
        reasons.append('missing tool guidance placeholder')
    descriptions = overlay.get('tool_descriptions') or {}
    if len(descriptions) > 64 or any(len(k) > 100 or len(v) > 4000 for k, v in descriptions.items()):
        reasons.append('tool description length limit')
    content = '\n'.join([overlay.get('system_prompt') or '', overlay.get('tool_guidance') or '', *descriptions.values()])
    if len(content) > 64000:
        reasons.append('total length limit')
    policy = re.compile(r'\b(ignore|bypass|permission\w*|sandbox\w*|unrestricted|disable\w*|override\w*)\b', re.IGNORECASE)

    def policy_lines(value: dict) -> dict[str, list[str]]:
        fields = {k: v for k, v in value.items() if isinstance(v, str)}
        fields.update({f'tool:{k}': v for k, v in (value.get('tool_descriptions') or {}).items()})
        return {key: lines for key, text in fields.items()
                if (lines := [line for line in text.splitlines() if policy.search(line)])}

    if policy_lines(overlay) != policy_lines(parent):
        reasons.append('permission or sandbox wording')

    def grams(value: str) -> set[tuple[str, ...]]:
        tokens = re.findall(r'\w+|[^\w\s]', value.casefold())
        # Punctuation-only runs (globs like `**/*.`, JSON escapes) are not benchmark content.
        windows = (tuple(tokens[i:i + 6]) for i in range(len(tokens) - 5))
        return {gram for gram in windows if sum(token[0].isalnum() for token in gram) >= 4}

    proposed = grams(content)
    if any(proposed & grams(value) for value in texts):
        reasons.append('benchmark text overlap')
    return {'valid': not reasons, 'reasons': reasons}


def metrics(run: dict) -> dict:
    rows = run['results']
    return {'pass_rate': mean(overall_passed(r) for r in rows) if rows else 0,
            'cost_usd': sum(float(r['cost_usd']) for r in rows),
            'turns': mean(r['turns'] for r in rows) if rows else 0}


def select_best(runs: list[dict]) -> dict | None:
    valid = [r for r in runs if r.get('status') == 'completed' and r['results']]
    return min(valid, key=lambda r: (-metrics(r)['pass_rate'], metrics(r)['cost_usd'], metrics(r)['turns'])) if valid else None


def paired_complete(a: dict, b: dict) -> bool:
    keys = lambda r: {(row['case_id'], row['repeat']) for row in r['results']}
    return (a.get('status') == b.get('status') == 'completed' and bool(a['results'])
            and keys(a) == keys(b) and len(keys(a)) == len(a['results']) == len(b['results']))


def dev_basis(a: dict, b: dict, min_cost_saving: float = .15) -> str | None:
    """Why a dev challenger beats the champion: more paired wins, or same quality for clearly less cost.

    The efficiency path lets evolution make progress when the benchmark is near its pass-rate ceiling.
    """
    if not paired_complete(a, b):
        return None
    report = compare(a, b)
    before, after = metrics(a), metrics(b)
    if after['pass_rate'] < before['pass_rate']:
        return None
    if report['B_wins'] > report['A_wins']:
        return 'quality'
    if report['A_wins'] == 0 and after['cost_usd'] <= before['cost_usd'] * (1 - min_cost_saving):
        return 'efficiency'
    return None


def gate(a: dict, b: dict, max_cost_increase: float = .25, *, dev: bool = False) -> bool:
    if dev:
        return dev_basis(a, b) is not None
    if not paired_complete(a, b):
        return False
    report = compare(a, b)
    before, after = metrics(a), metrics(b)
    return (after['pass_rate'] >= before['pass_rate'] and report['B_wins'] >= report['A_wins']
            and after['cost_usd'] <= before['cost_usd'] * (1 + max_cost_increase))
