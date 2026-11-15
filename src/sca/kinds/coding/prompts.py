"""Compatibility entry points for versioned coding templates."""

from sca.kernel.prompts import render_prompt


def initial_prompt(instruction: str, verify_command: str | list[str] | None,
                   prompt_version: str | None = None) -> str:
    commands = [verify_command] if isinstance(verify_command, str) else list(verify_command or [])
    return render_prompt({'instruction': instruction, 'verify_commands': commands, 'prompt_version': prompt_version}, 'initial')


def fix_prompt(verify_command: str, exit_code: int, output_tail: str,
               prompt_version: str | None = None) -> str:
    return render_prompt({'verify_command': verify_command, 'prompt_version': prompt_version}, 'fix',
                         exit_code=exit_code, output=output_tail)


def ci_fix_prompt(summary: str, url: str | None, prompt_version: str | None = None) -> str:
    return render_prompt({'prompt_version': prompt_version}, 'ci_fix', summary=summary.strip() or '(no details given)',
                         where=render_prompt({'prompt_version': prompt_version}, 'ci_details', url=url) if url else '')


def feedback_prompt(feedback: str, prompt_version: str | None = None) -> str:
    return render_prompt({'prompt_version': prompt_version}, 'feedback', feedback=feedback)


def planning_prompt(instruction: str, feedback: str | None = None,
                    prompt_version: str | None = None) -> str:
    return render_prompt({'instruction': instruction, 'prompt_version': prompt_version}, 'planning',
                         reviewer_feedback=render_prompt({'prompt_version': prompt_version}, 'planning_feedback', feedback=feedback) if feedback else '')


def subtask_prompt(spec: dict, subtask: dict) -> str:
    return render_prompt(spec, "subtask", subtask_name=subtask["name"], subtask_instruction=subtask["instruction"])
