"""Persist the local Portal switch job across API restarts."""

import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from sca.gateway.model_providers import atomic_write
from sca.host import lock_file, unlock_file
from sca.runtime_mode import PREFERENCE_FILE, RuntimeMode, preferred_mode

SwitchStep = Literal["queued", "export", "stop", "start", "import", "health", "rollback", "done"]
SwitchState = Literal["running", "succeeded", "failed"]
STEP_NAMES: dict[SwitchStep, str] = {
    "queued": "等待切换",
    "export": "导出历史记录",
    "stop": "停止原运行模式",
    "start": "启动目标模式",
    "import": "导入历史记录",
    "health": "检查服务健康状态",
    "rollback": "恢复原运行模式",
    "done": "切换完成",
}


class SwitchStatus(BaseModel):
    id: UUID
    status: SwitchState = "running"
    step: SwitchStep = "queued"
    log_tail: str = STEP_NAMES["queued"]
    error: str | None = None


class SwitchRecord(SwitchStatus):
    source: RuntimeMode
    target: RuntimeMode
    carry_history: bool
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    def save(self, data: Path) -> None:
        atomic_write(data / "run/runtime-switch.json", self.model_dump_json() + "\n")

    def report(
        self, data: Path, step: SwitchStep, *, status: SwitchState = "running", error: str | None = None,
    ) -> None:
        self.step = step
        self.status = status
        self.error = error
        self.updated_at = datetime.now(UTC)
        # Launcher and history CLI output can contain credentials or user data.
        self.log_tail = "\n".join([*self.log_tail.splitlines(), error or STEP_NAMES[step]][-10:])
        self.save(data)


def read_switch(data: Path) -> SwitchRecord | None:
    try:
        return SwitchRecord.model_validate_json((data / "run/runtime-switch.json").read_bytes())
    except (FileNotFoundError, ValidationError):
        return None


def save_preference(data: Path, mode: RuntimeMode | None) -> None:
    path = data / PREFERENCE_FILE
    if mode is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, json.dumps({"mode": mode}) + "\n")


@contextmanager
def switch_lock(data: Path, *, blocking: bool) -> Iterator[None]:
    path = data / "run/runtime-switch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        lock_file(stream, blocking=blocking)
        try:
            yield
        finally:
            unlock_file(stream)


def launch_switch(root: Path, switch_id: UUID) -> None:
    options = {"start_new_session": True}
    if sys.platform == "win32":
        options = {"creationflags": (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                     | subprocess.CREATE_BREAKAWAY_FROM_JOB)}
    subprocess.Popen(
        [sys.executable, "-X", "utf8", str(root / "scripts/portal_switch.py"), str(switch_id)],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **options,
    )


def begin_switch(root: Path, source: RuntimeMode, target: RuntimeMode, carry_history: bool) -> UUID:
    data = root / ".data"
    with switch_lock(data, blocking=False):
        previous = read_switch(data)
        # The helper owns the lock once running. Allow it time to acquire the lock after spawn.
        if (previous is not None and previous.status == "running" and previous.step == "queued"
                and datetime.now(UTC) - previous.updated_at < timedelta(seconds=30)):
            raise BlockingIOError
        preference = preferred_mode(data)
        record = SwitchRecord(id=uuid4(), source=source, target=target, carry_history=carry_history)
        record.save(data)
        try:
            save_preference(data, target)
            launch_switch(root, record.id)
        except OSError:
            save_preference(data, preference)
            record.report(data, "queued", status="failed", error="无法启动运行模式切换。")
            raise
    return record.id
