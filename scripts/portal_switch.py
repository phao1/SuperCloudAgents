"""Switch a local Portal installation between runtime modes."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from uuid import UUID

from sca.runtime_mode import RuntimeMode
from sca.runtime_switch import STEP_NAMES, SwitchRecord, read_switch, save_preference, switch_lock

# These are set by load_source_env in portal_source.sh. Docker must load its own values.
_SOURCE_OVERRIDES = (
    "SCA_DATA_DIR", "SCA_DATABASE_URL", "SCA_SANDBOX_DRIVER", "SCA_STATE_TRANSPORT",
    "SCA_TEMPORAL_ADDRESS", "SCA_MODEL_GATEWAY_URL", "SCA_OPENCODE_DEFAULT_VERSION",
    "SCA_OPENCODE_VERSIONS_DIR",
)
_LAUNCH_ERRORS = (OSError, subprocess.SubprocessError)


def launch(
    root: Path, source: RuntimeMode, mode: RuntimeMode, command: str, archive: Path | None = None,
) -> None:
    env = {**os.environ, "SCA_RUNTIME_MODE": mode}
    if source == "source" and mode == "docker":
        for name in _SOURCE_OVERRIDES:
            env.pop(name, None)
    if sys.platform == "win32":
        argv = [sys.executable, "-X", "utf8", str(root / "scripts/portal_source.py"), command]
    else:
        argv = ["bash", str(root / "scripts/portal_demo.sh"), command]
        if command == "down" and mode == "docker":
            argv.append("--all")
        elif command != "down":
            argv.extend(("--mode", mode))
    if archive is not None:
        argv.append(str(archive))
    subprocess.run(argv, cwd=root, env=env, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_health() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        http = HTTPConnection("127.0.0.1", 8000, timeout=2)
        try:
            http.request("GET", "/healthz")
            if http.getresponse().status == 200:
                return
        except (OSError, HTTPException):
            pass
        finally:
            http.close()
        time.sleep(1)
    raise TimeoutError


def restore_original(root: Path, record: SwitchRecord, *, target_attempted: bool) -> str:
    notes = []
    if target_attempted:
        try:
            launch(root, record.source, record.target, "down")
        except _LAUNCH_ERRORS:
            notes.append("停止目标模式失败。")
    try:
        launch(root, record.source, record.source, "up")
    except _LAUNCH_ERRORS:
        notes.append("恢复原运行模式失败，请检查 .data/run/ 中的服务日志。")
    else:
        notes.append("已恢复原运行模式。")
    try:
        save_preference(root / ".data", record.source)
    except OSError:
        notes.append("无法恢复原启动首选项。")
    return "".join(notes)


def run_switch(root: Path, record: SwitchRecord) -> bool:
    data = root / ".data"
    archive = data / "switch" / f"{record.id}.zip"
    target_attempted = False
    try:
        if record.carry_history:
            record.report(data, "export")
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            launch(root, record.source, record.source, "export-history", archive)
            if not archive.is_file():
                raise FileNotFoundError
        record.report(data, "stop")
        launch(root, record.source, record.source, "down")
        record.report(data, "start")
        target_attempted = True
        launch(root, record.source, record.target, "up")
        if record.carry_history:
            record.report(data, "import")
            launch(root, record.source, record.target, "import-history", archive)
        record.report(data, "health")
        wait_for_health()
    except _LAUNCH_ERRORS as exc:
        failed = record.step
        code = f"（退出码 {exc.returncode}）" if isinstance(exc, subprocess.CalledProcessError) else ""
        with suppress(OSError):
            record.report(data, "rollback")
        recovery = restore_original(root, record, target_attempted=target_attempted)
        record.report(data, failed, status="failed", error=f"{STEP_NAMES[failed]}失败{code}。{recovery}")
        return False

    if record.carry_history and os.environ.get("SCA_KEEP_SWITCH_ARCHIVE") != "1":
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            record.log_tail += "\n未能清理历史 ZIP，文件保留在 .data/switch/。"
    record.report(data, "done", status="succeeded")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", type=UUID)
    switch_id = parser.parse_args().id
    root = Path(__file__).resolve().parents[1]
    data = root / ".data"
    with switch_lock(data, blocking=True):
        record = read_switch(data)
        if record is None or record.id != switch_id or record.step != "queued" or record.status != "running":
            return 1
        return 0 if run_switch(root, record) else 1


if __name__ == "__main__":
    raise SystemExit(main())
