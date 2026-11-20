"""Turn a failed task's clean Git base into a reviewable evaluation case."""

import asyncio
import os
import shlex
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sca.config import Settings
from sca.core.errors import ConflictError, InvalidTaskError, WorkspaceError
from sca.core.models import EventType, RepoCredential, Task, TaskState
from sca.db.repo import Store
from sca.eval.suites import Split
from sca.evolve.models import SuiteName
from sca.kinds.coding.spec import CodingSpec
from sca.secrets import make_secret_provider
from sca.workspace.git import GitWorkspace


class HarvestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    suite: SuiteName = "opencode-harvested"
    split: Split
    acceptance_command: str | None = Field(default=None, min_length=1)
    notes: str = Field(default="", max_length=4000)


async def rejected_by_reviewer(task: Task, store: Store) -> bool:
    if task.state != TaskState.CANCELLED:
        return False
    decision = None
    after = 0
    while batch := await store.list_events(task.id, after=after):
        for event in batch:
            if event.type == EventType.REVIEW_SUBMITTED:
                decision = event.payload.get("decision")
            elif event.type == EventType.TASK_CANCEL_REQUESTED:
                decision = None
        after = batch[-1].seq
    return decision == "reject"


def write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


async def snapshot_repository(settings: Settings, task: Task, spec: CodingSpec, target: Path) -> str:
    source = settings.task_dir(str(task.id)) / "mirror.git"
    base_ref = task.result.get("base_sha") or spec.base_ref
    with TemporaryDirectory(prefix=".git-base-", dir=target.parent) as directory:
        scratch = GitWorkspace(Path(directory), "main")
        try:
            if source.is_dir() and not source.is_symlink():
                try:
                    base = (await scratch._git(
                        "--git-dir", str(source), "rev-parse", "--verify", "--end-of-options",
                        f"{base_ref}^{{commit}}",
                    )).strip()
                except WorkspaceError:
                    source = scratch.mirror
            else:
                source = scratch.mirror
            if source == scratch.mirror:
                credential = await make_secret_provider(settings).repo_credential(
                    task.tenant_id, spec.repo_url,
                )
                if credential is None and settings.secret_provider == "openbao":
                    credential = RepoCredential(username="", password="")
                base = (await scratch.prepare(
                    spec.repo_url, base_ref, mirror_only=True, credential=credential,
                    tenant_id=task.tenant_id,
                )).base_sha
            # Check out from the trusted bare mirror into a fresh directory outside the task.
            await scratch._git("clone", "--shared", "--no-checkout", "--", str(source), str(target))
            await scratch._git("-C", str(target), "checkout", "--detach", base)
            shutil.rmtree(target / ".git")
        except (InvalidTaskError, WorkspaceError, OSError):
            raise InvalidTaskError("无法读取任务仓库的基线提交") from None
    if any(path.is_symlink() for path in target.rglob("*")):
        raise InvalidTaskError("评测套件中的仓库快照不能包含符号链接")
    return base


def write_case(
    target: Path, case_id: str, split: Split, spec: CodingSpec, acceptance: list[str],
) -> None:
    task = {"split": split, "category": "harvested", "base_ref": "main",
            "instruction": spec.instruction, "verify_command": spec.verify_command,
            "verify_commands": spec.verify_commands, "setup_command": spec.setup_command}
    write_yaml(target / "task.yaml", task)
    script = f".sca-{case_id}/check.sh"
    wrapper = target / "acceptance" / script
    wrapper.parent.mkdir(parents=True)
    commands = [spec.setup_command, *acceptance]
    wrapper.write_text("set -e\n" + "".join(
        shlex.join(["bash", "-lc", command]) + "\n" for command in commands if command
    ), encoding="utf-8")
    write_yaml(target / "acceptance.yaml", {"command": f"bash {script}"})


async def harvest(settings: Settings, store: Store, body: HarvestRequest, actor: str) -> dict:
    task = await store.get_task(body.task_id)
    if task.kind != "coding" or task.spec.get("eval_run_id"):
        raise InvalidTaskError("只能采集真实的编码任务")
    if task.state != TaskState.FAILED and not await rejected_by_reviewer(task, store):
        raise ConflictError("只能采集失败或被人工拒绝的任务")
    spec = CodingSpec.model_validate(task.spec)
    acceptance = [body.acceptance_command] if body.acceptance_command is not None else spec.checks()
    if not acceptance or not all(command.strip() for command in acceptance):
        raise InvalidTaskError("请提供验收命令，或选择带验证命令的任务")
    candidate_id = uuid4()
    case_id = f"harvest-{candidate_id.hex}"
    root = settings.data_dir.resolve() / "harvest"
    root.mkdir(parents=True, exist_ok=True)
    target = root / str(candidate_id)
    with TemporaryDirectory(prefix=".candidate-", dir=root) as directory:
        staging = Path(directory) / "case"
        staging.mkdir()
        base_sha = await snapshot_repository(settings, task, spec, staging / "repo")
        write_case(staging, case_id, body.split, spec, acceptance)
        staging.rename(target)
    try:
        return await store.create_harvest_candidate({
            "id": candidate_id, "task_id": task.id, "suite": body.suite, "split": body.split,
            "case_id": case_id, "base_sha": base_sha, "notes": body.notes, "created_by": actor,
        })
    except Exception:
        await asyncio.to_thread(shutil.rmtree, target)
        raise


def publish_case(settings: Settings, candidate: dict) -> None:
    source = settings.data_dir.resolve() / "harvest" / str(candidate["id"])
    if not source.is_dir() or source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
        raise ConflictError("采集候选的文件不存在或无效")
    root = settings.eval_suites_dir.resolve()
    suite = root / candidate["suite"]
    cases = suite / "cases"
    suite_yaml = suite / "suite.yaml"
    target = cases / candidate["case_id"]
    for path in (suite, cases, suite_yaml, target):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ConflictError("评测套件路径无效")
    if target.is_dir():
        # A retry can follow a successful rename with no committed review row.
        return
    try:
        task = yaml.safe_load((source / "task.yaml").read_text(encoding="utf-8"))
        if not isinstance(task, dict) or task.get("split") != candidate["split"]:
            raise ConflictError("采集候选的评测分组已变更")
        cases.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".harvest-", dir=root) as directory:
            staging = Path(directory)
            if not suite_yaml.exists():
                metadata = staging / "suite.yaml"
                write_yaml(metadata, {"name": candidate["suite"], "kind": "coding"})
                try:
                    os.link(metadata, suite_yaml)
                except FileExistsError:
                    pass
            suite_spec = yaml.safe_load(suite_yaml.read_text(encoding="utf-8"))
            if not isinstance(suite_spec, dict) or suite_spec.get("kind", "coding") != "coding":
                raise ConflictError("请选择编码评测套件")
            copy = staging / "case"
            shutil.copytree(source, copy)
            copy.rename(target)
    except (OSError, yaml.YAMLError):
        raise ConflictError("无法写入评测套件") from None
