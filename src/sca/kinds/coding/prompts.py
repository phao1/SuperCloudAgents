"""Prompt templates for coding tasks (design.md §7.4).

Pure functions with stdlib-only imports: the workflow calls them directly.
"""

from __future__ import annotations

_RULES = """\
Rules:
- Work only inside the current directory, which is a git checkout of the repository.
- Do not commit, push, or change git configuration; the platform records your changes.
- When you are done, reply with a short summary of what you changed and why."""


def initial_prompt(instruction: str, verify_command: str | list[str] | None) -> str:
    commands = [verify_command] if isinstance(verify_command, str) else list(verify_command or [])
    if len(commands) == 1:
        verify = (
            "Your work will be accepted when this command exits with status 0, "
            f"run from the repository root:\n\n    {commands[0]}\n\n"
            "Run it yourself before you finish."
        )
    elif commands:
        listing = "\n".join(f"    {c}" for c in commands)
        verify = (
            "Your work will be accepted when every one of these commands exits with status 0, "
            f"each run from the repository root:\n\n{listing}\n\n"
            "Run them yourself before you finish."
        )
    else:
        verify = "No verification command was given; check your work as you see fit."
    return f"# Task\n\n{instruction.strip()}\n\n# Verification\n\n{verify}\n\n{_RULES}\n"


def fix_prompt(verify_command: str, exit_code: int, output_tail: str) -> str:
    return (
        "The verification command failed after your last change.\n\n"
        f"Command: {verify_command}\nExit status: {exit_code}\n\n"
        f"End of its output:\n\n```\n{output_tail.strip()}\n```\n\n"
        "Find the cause and fix it so the command passes. Do not weaken or delete tests to "
        f"make it pass.\n\n{_RULES}\n"
    )


def ci_fix_prompt(summary: str, url: str | None) -> str:
    where = f"Details: {url}\n\n" if url else ""
    return (
        "The project's CI pipeline failed on your last change.\n\n"
        f"What it reported:\n\n```\n{summary.strip() or '(no details given)'}\n```\n\n{where}"
        "Find the cause and fix it. Do not weaken or delete tests to make it pass.\n\n"
        f"{_RULES}\n"
    )


def feedback_prompt(feedback: str) -> str:
    return (
        "A reviewer looked at your changes and asked for the following:\n\n"
        f"{feedback.strip()}\n\nMake those changes.\n\n{_RULES}\n"
    )
