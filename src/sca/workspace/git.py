from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sca.core.errors import InvalidTaskError, WorkspaceError
from sca.workspace.cache import RepoCache

DEFAULT_EXCLUDES = (
    ".sca/",
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".venv/",
    "node_modules/",
    ".DS_Store",
)


@dataclass(frozen=True)
class Prepared:
    base_sha: str
    branch: str
    workspace_path: Path


class GitWorkspace:
    def __init__(self, task_dir: Path, branch: str, cache: RepoCache | None = None):
        self.cache = cache
        self.task_dir = task_dir.resolve()
        self.branch = branch
        self.mirror = self.task_dir / "mirror.git"
        self.workspace = self.task_dir / "workspace"

    async def _git(self, *args: str, invalid: bool = False) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *args,
                cwd=self.task_dir,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
        except OSError as exc:
            raise WorkspaceError(str(exc)) from exc
        if proc.returncode:
            error = InvalidTaskError if invalid else WorkspaceError
            raise error(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
        return out.decode(errors="replace")

    async def prepare(self, repo_url: str, base_ref: str, *, mirror_only: bool = False) -> Prepared:
        try:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            (self.task_dir / "workspace.ready").unlink(missing_ok=True)
            for path in (self.workspace, self.mirror):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
        except OSError as exc:
            raise WorkspaceError(f"Could not reset workspace: {exc}") from exc
        if self.cache is None:
            await self._git("clone", "--mirror", "--", repo_url, str(self.mirror), invalid=True)
        else:
            async with self.cache.mirror(repo_url) as cached:
                await self._git(
                    "clone",
                    "--mirror",
                    "--reference",
                    str(cached),
                    "--dissociate",
                    "--",
                    str(cached),
                    str(self.mirror),
                    invalid=True,
                )
            await self._git(
                "--git-dir",
                str(self.mirror),
                "remote",
                "set-url",
                "origin",
                repo_url,
            )
            await self._git(
                "--git-dir",
                str(self.mirror),
                "fetch",
                "--prune",
                "origin",
                invalid=True,
            )
        base = await self._git(
            "--git-dir",
            str(self.mirror),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{base_ref}^{{commit}}",
            invalid=True,
        )
        if mirror_only:
            return Prepared(base.strip(), self.branch, self.workspace)
        return await self.build_workspace(base.strip())

    async def build_workspace(self, commit: str) -> Prepared:
        ready = self.task_dir / "workspace.ready"
        if ready.exists():
            return Prepared(commit, self.branch, self.workspace)
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        await self._git("clone", "--no-local", "--", str(self.mirror), str(self.workspace))
        # Preparation is the only time the host touches the trusted, freshly cloned checkout.
        await self._git("-C", str(self.workspace), "checkout", "-b", self.branch, commit)
        # Running the verify command leaves caches behind; a repo without a .gitignore
        # would otherwise get them committed into the result.
        exclude = self.workspace / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n".join(["", *DEFAULT_EXCLUDES, ""]))
        ready.write_text(commit)
        return Prepared(commit, self.branch, self.workspace)

    def commit_argv(self, message: str) -> list[str]:
        script = (
            "git -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A && "
            "if ! git -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --cached --quiet; "
            "then git -c core.hooksPath=/dev/null -c core.fsmonitor=false "
            "-c user.name=SuperCloudAgents -c user.email=agents@supercloud.invalid "
            '-c commit.gpgsign=false commit -m "$1"; fi'
        )
        identity = "SuperCloudAgents"
        return [
            "env",
            f"GIT_AUTHOR_NAME={identity}",
            "GIT_AUTHOR_EMAIL=agents@supercloud.invalid",
            f"GIT_COMMITTER_NAME={identity}",
            "GIT_COMMITTER_EMAIL=agents@supercloud.invalid",
            "bash",
            "-lc",
            script,
            "sca-commit",
            message,
        ]

    async def fetch(self) -> str:
        await self._git(
            "--git-dir",
            str(self.mirror),
            "fetch",
            "--no-tags",
            "--upload-pack=git -c core.hooksPath=/dev/null -c core.fsmonitor=false upload-pack",
            "--",
            str(self.workspace),
            f"+HEAD:refs/heads/{self.branch}",
        )
        return (
            await self._git("--git-dir", str(self.mirror), "rev-parse", f"refs/heads/{self.branch}")
        ).strip()

    async def diff(self, base_sha: str) -> str:
        return await self._git(
            "--git-dir",
            str(self.mirror),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            base_sha,
            f"refs/heads/{self.branch}",
            "--",
        )

    async def changed(self, base_sha: str) -> bool:
        return bool(await self.diff(base_sha))

    async def push(self, repo_url: str) -> None:
        await self._git(
            "--git-dir",
            str(self.mirror),
            "push",
            "--force",
            "--",
            repo_url,
            f"refs/heads/{self.branch}:refs/heads/{self.branch}",
        )
