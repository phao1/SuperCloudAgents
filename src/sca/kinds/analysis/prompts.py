"""Compatibility entry points for versioned analysis templates."""

from sca.kernel.prompts import render_prompt


def initial_prompt(spec: dict) -> str:
    return render_prompt({**spec, '_kind': 'analysis'}, 'initial')


def fix_prompt(exit_code: int, output: str) -> str:
    return render_prompt({'_kind': 'analysis'}, 'fix', exit_code=exit_code, output=output)


def feedback_prompt(feedback: str) -> str:
    return render_prompt({'_kind': 'analysis'}, 'feedback', feedback=feedback)
