"""Pure static checks, ranking and paired promotion gates."""

import hashlib
import json
import re
from math import isfinite
from statistics import mean

from sca.eval.compare import compare
from sca.eval.trajectory import is_errored, overall_passed
from sca.evolve.models import Overlay


def overlay_hash(overlay: dict) -> str:
    return hashlib.sha256(json.dumps(overlay, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def static_check(
    overlay: dict, texts: list[str], surfaces: list[str], parent: dict | None = None, *,
    agent_backend: str = 'opencode',
) -> dict:
    reasons = []
    try:
        Overlay.model_validate(overlay)
    except ValueError:
        return {'valid': False, 'reasons': ['invalid overlay']}
    parent = parent or {}
    if any(overlay.get(key) != parent.get(key) for key in overlay.keys() | parent.keys() if key not in surfaces):
        reasons.append('surface is not permitted')
    prompt = overlay.get('system_prompt')
    if agent_backend == 'opencode' and prompt is not None and '${OPENCODE_TOOL_GUIDANCE}' not in prompt:
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
    scored = [r for r in rows if not is_errored(r)]
    return {'pass_rate': mean(overall_passed(r) for r in scored) if scored else 0,
            'cost_usd': sum(float(r['cost_usd']) for r in rows),
            'turns': mean(r['turns'] for r in rows) if rows else 0}


def select_best(runs: list[dict]) -> dict | None:
    valid = [r for r in runs if r.get('status') == 'completed' and r['results']]
    return min(valid, key=lambda r: (-metrics(r)['pass_rate'], metrics(r)['cost_usd'], metrics(r)['turns'])) if valid else None


def paired_complete(a: dict, b: dict, *, legacy: bool = False) -> bool:
    keys = lambda r: {(row['case_id'], row['repeat']) for row in r['results']}
    return (a.get('status') == b.get('status') == 'completed' and bool(a['results'])
            and keys(a) == keys(b) and len(keys(a)) == len(a['results']) == len(b['results'])
            and (legacy or not any(is_errored(row) for run in (a, b) for row in run['results'])))


def measured_cost(cost: float) -> bool:
    return isfinite(cost) and cost > 0


def dev_basis(a: dict, b: dict, min_cost_saving: float = .15, *, legacy: bool = False) -> str | None:
    """Why a dev challenger beats the champion: more paired wins, or same quality for clearly less cost.

    The efficiency path lets evolution make progress when the benchmark is near its pass-rate ceiling.
    """
    if not paired_complete(a, b, legacy=legacy):
        return None
    report = compare(a, b)
    before, after = metrics(a), metrics(b)
    if after['pass_rate'] < before['pass_rate'] or (not legacy and before['cost_usd'] == after['cost_usd'] == 0):
        return None
    if report['B_wins'] > report['A_wins']:
        return 'quality'
    costs_known = legacy or (measured_cost(before['cost_usd']) and measured_cost(after['cost_usd']))
    if costs_known and report['A_wins'] == 0 and after['cost_usd'] <= before['cost_usd'] * (1 - min_cost_saving):
        return 'efficiency'
    return None


def gate(a: dict, b: dict, max_cost_increase: float = .25, *, dev: bool = False, legacy: bool = False) -> bool:
    if dev:
        return dev_basis(a, b, legacy=legacy) is not None
    if not paired_complete(a, b, legacy=legacy):
        return False
    report = compare(a, b)
    before, after = metrics(a), metrics(b)
    costs_known = legacy or (measured_cost(before['cost_usd']) and measured_cost(after['cost_usd']))
    return (costs_known and after['pass_rate'] >= before['pass_rate'] and report['B_wins'] >= report['A_wins']
            and after['cost_usd'] <= before['cost_usd'] * (1 + max_cost_increase))


def invalid_run_reason(run: dict) -> str | None:
    if run['status'] == 'failed':
        return f"评测 {run['id']} 失败：{run.get('reason') or '请查看评测记录'}"
    if run['status'] == 'completed':
        rows = run['results']
        if not rows:
            return f"评测 {run['id']} 没有可用的配对结果"
        if errors := sum(is_errored(row) for row in rows):
            return f"评测 {run['id']} 有 {errors}/{len(rows)} 个执行错误，无法完成配对评测"
    return None
