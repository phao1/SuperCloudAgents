"""Prepare and update the local checkout selected for a new coding task."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sca.config import Settings
from sca.core.errors import InvalidTaskError, WorkspaceError
from sca.core.models import Task
from sca.host import lock_file

_SCA_ROOT = Path(__file__).resolve().parents[3]
_GIT_CONFIG = (
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("commit.gpgsign", "false"),
    ("maintenance.auto", "false"),
    ("submodule.recurse", "false"),
)


@dataclass(frozen=True)
class OutputTarget:
    branch: str
    head: str
    base: str
    subject: str


def _data_roots(settings: Settings) -> set[Path]:
    return {settings.data_dir.resolve(), (_SCA_ROOT / ".data").resolve()}


def _in_data(path: Path, settings: Settings) -> bool:
    projects = settings.projects_root
    return any(
        path.is_relative_to(root)
        and not (projects != root and projects.is_relative_to(root) and path.is_relative_to(projects))
        for root in _data_roots(settings)
    )


def _system_path(path: Path) -> bool:
    if os.name == "nt":
        roots = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
        return any(path.is_relative_to(Path(root).resolve()) for key in roots
                   if (root := os.environ.get(key)))
    root = Path(path.anchor)
    system = ("bin", "boot", "dev", "etc", "lib", "lib64", "proc", "run", "sbin", "sys", "usr",
              "Applications", "Library", "System", "private/etc")
    return path in (root / "var", root / "private/var") or any(
        path.is_relative_to(root / name) for name in system
    )


def _output_path(raw: str, settings: Settings) -> Path:
    path = Path(raw)
    if (not path.is_absolute() or any(ord(char) < 32 for char in raw)
            or (os.name == "nt" and (path.drive.startswith("\\") or any(
                Path(part).is_reserved() or part.endswith((" ", ".")) or ":" in part
                for part in path.parts[1:] if part != ".."
            )))):
        raise InvalidTaskError("输出目录必须是有效的本机绝对路径")
    resolved = path.resolve()
    protected = {Path.home().resolve(), _SCA_ROOT, *_data_roots(settings)}
    if (any(root.is_relative_to(resolved) for root in protected) or resolved == Path(resolved.anchor)
            or _system_path(resolved) or _in_data(resolved, settings)
            or any(part.casefold() == ".git" for part in resolved.parts)):
        raise InvalidTaskError("不能使用系统目录、主目录、SCA 仓库或 SCA 数据目录作为输出目录")
    return resolved


@contextmanager
def _output_lock(settings: Settings, path: Path) -> Iterator[None]:
    root = settings.data_dir / "output-locks"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    # Keep the lock file so concurrent users of the path always open the same file.
    with (root / f"{key}.lock").open("a+b") as stream:
        lock_file(stream)
        yield


class _Git:
    def __init__(self, path: Path, git_dir: Path | None = None) -> None:
        self.location = ["-C", path.as_posix()]
        if git_dir is not None:
            self.location += [f"--git-dir={git_dir.as_posix()}", f"--work-tree={path.as_posix()}"]
        self.config = list(_GIT_CONFIG)

    def run(
        self, *args: str, check: bool = True, author: str = "SCA", date: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_NO_REPLACE_OBJECTS="1", GIT_AUTHOR_NAME=author, GIT_COMMITTER_NAME=author,
                   GIT_AUTHOR_EMAIL="sca@localhost", GIT_COMMITTER_EMAIL="sca@localhost")
        if date is not None:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        config = [arg for key, value in self.config for arg in ("-c", f"{key}={value}")]
        return subprocess.run(
            ["git", *config, *self.location, *args], check=check, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120,
            encoding="utf-8", errors="replace",
        )

    def ref(self, name: str) -> str:
        return self.run("rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}",
                        check=False).stdout.strip()

    def branch(self) -> str:
        return self.run("symbolic-ref", "--quiet", "HEAD",
                        check=False).stdout.strip().removeprefix("refs/heads/")

    def clean(self) -> bool:
        return not self.run("status", "--porcelain=v1", "--untracked-files=all",
                            "--ignore-submodules=none").stdout

    def disable_filters(self) -> None:
        # Checkout and status must not execute configured filters on agent-controlled files.
        keys = self.run("config", "--null", "--name-only", "--get-regexp", r"^filter\.",
                        check=False).stdout.split("\0")
        for name in sorted({key.rpartition(".")[0] for key in keys if key}):
            self.config += [(f"{name}.{key}", value) for key, value in (
                ("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"),
            )]

    def is_ancestor(self, commit: str, tip: str) -> bool:
        return bool(tip) and self.run("merge-base", "--is-ancestor", commit, tip,
                                      check=False).returncode == 0


def _init_checkout(path: Path) -> None:
    nonempty = any(path.iterdir())
    with tempfile.TemporaryDirectory(prefix=".sca-git-", dir=path) as staging:
        git_dir = Path(staging) / "git"
        git = _Git(path, git_dir)
        git.run("init", "--quiet", "--template=", "-b", "main")
        if nonempty:
            git.run("add", "--all", "--", ".", f":(exclude,top){Path(staging).name}")
        git.run("commit", "--quiet", "--allow-empty", "-m",
                "Import existing files" if nonempty else "Initial empty commit")
        # Once installed as .git, the repository should follow its enclosing directory.
        if git.run("config", "--local", "--get", "core.worktree", check=False).returncode == 0:
            git.run("config", "--local", "--unset-all", "core.worktree")
        git_dir.rename(path / ".git")


def _checkout(path: Path, settings: Settings, *, create: bool = False) -> _Git:
    git = _Git(path)
    toplevel = git.run("rev-parse", "--show-toplevel", check=False).stdout.rstrip("\r\n")
    if not toplevel or Path(toplevel).resolve() != path:
        marker = path / ".git"
        bare = git.run("rev-parse", "--is-bare-repository", check=False).stdout.strip() == "true"
        if not create or marker.exists() or marker.is_symlink() or bare:
            raise InvalidTaskError("输出目录必须是有工作区的 Git 仓库根目录")
        _init_checkout(path)
    common = Path(git.run("rev-parse", "--path-format=absolute",
                          "--git-common-dir").stdout.rstrip("\r\n")).resolve()
    sca_git = _Git(_SCA_ROOT).run("rev-parse", "--path-format=absolute", "--git-common-dir",
                                 check=False).stdout.rstrip("\r\n")
    if (_in_data(common, settings) or _system_path(common)
            or (sca_git and common == Path(sca_git).resolve())):
        raise InvalidTaskError("不能使用 SCA 仓库或 SCA 数据目录作为输出目录")
    git.disable_filters()
    return git


def prepare_output(
    settings: Settings, raw_path: str | None, instruction: str, base_ref: str | None,
) -> tuple[str, str, OutputTarget]:
    subject = next(iter(instruction.splitlines()), "").strip() or "SCA task"
    slug = re.sub(r"[\W_]+", "-", unicodedata.normalize("NFKC", subject).lower()).strip("-")[:48]
    try:
        if raw_path is None:
            root = _output_path(settings.projects_root.as_posix(), settings)
            root.mkdir(parents=True, exist_ok=True)
            # A default path belongs to this submission even if another project has the same title.
            while True:
                path = root / f"{slug.rstrip('-') or 'project'}-{uuid4().hex[:6]}"
                try:
                    path.mkdir()
                    break
                except FileExistsError:
                    continue
        else:
            path = _output_path(raw_path, settings)
        with _output_lock(settings, path):
            path.mkdir(parents=True, exist_ok=True)
            git = _checkout(path, settings, create=True)
            branch = git.branch()
            if not branch:
                raise InvalidTaskError("输出目录必须检出一个 Git 分支")
            if not git.clean():
                raise InvalidTaskError("输出目录的 Git 工作区有未提交的更改，请先提交或使用 git stash")
            head = git.ref("HEAD")
            if not head:
                git.run("commit", "--quiet", "--allow-empty", "-m", "Initial empty commit")
                head = git.ref("HEAD")
            base = git.ref(base_ref) if base_ref is not None else head
            if not base:
                raise InvalidTaskError("输出目录的基线引用不存在")
        return path.as_posix(), base_ref or branch, OutputTarget(branch, head, base, subject)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        raise InvalidTaskError("无法准备输出目录，请检查路径、Git 和写入权限") from None


def write_output(settings: Settings, task: Task, mirror: Path) -> dict:
    target = OutputTarget(**task.result["output_target"])
    try:
        path = _output_path(task.spec["output_dir"], settings)
        if path != Path(task.spec["output_dir"]):
            raise InvalidTaskError("输出目录已被移动或替换")
        with _output_lock(settings, path):
            git = _checkout(path, settings)
            source = _Git(mirror)
            base = task.result["base_sha"]
            tree = source.run("rev-parse", "--verify", f'{task.result["commit"]}^{{tree}}').stdout.strip()
            # A retry creates the same single commit from the same verified tree.
            commit = source.run(
                "commit-tree", tree, "-p", base, "-m", target.subject,
                "-m", f"Sca-Task-Id: {task.id}", author="SCA Agent", date=task.created_at.isoformat(),
            ).stdout.strip()
            git.run("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                    "--", mirror.as_posix(), commit)
            branch = _deliver_commit(git, target, commit, task.id.hex)
        return {"output_dir": path.as_posix(), "output_commit": commit, "output_branch": branch,
                "output_on_side_branch": branch != target.branch}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, InvalidTaskError):
        raise WorkspaceError("无法将代码写入输出目录，请检查路径、Git 和写入权限") from None


def _deliver_commit(git: _Git, target: OutputTarget, commit: str, task_hex: str) -> str:
    sides = (f"sca/{task_hex[:8]}", f"sca/{task_hex}")
    original = git.ref(f"refs/heads/{target.branch}")
    if git.is_ancestor(commit, original):
        return target.branch
    for side in sides:
        if git.is_ancestor(commit, git.ref(f"refs/heads/{side}")):
            return side
    if (original == target.head and git.branch() == target.branch and git.clean()
            and git.run("merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", commit,
                        check=False).returncode == 0):
        return target.branch
    for side in sides:
        if git.run("update-ref", f"refs/heads/{side}", commit, "", check=False).returncode == 0:
            return side
    raise WorkspaceError("无法在输出目录创建结果分支")
