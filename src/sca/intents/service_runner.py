"""Sandbox-only service lifetime and readiness wrapper."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


def start_service(command: str) -> subprocess.Popen:
    if sys.platform == "win32":
        from sca.host import windows_bash

        return subprocess.Popen(
            [sys.executable, "-m", "sca.sandbox._windows_job", str(windows_bash()),
             "--noprofile", "--norc", "-c", command],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
    return subprocess.Popen(["bash", "-lc", command], start_new_session=True)


def stop_service(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            if process.poll() is None:
                process.kill()
            process.wait()
            return
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as stream:
        config = json.load(stream)
    process = start_service(config["start_command"])
    try:
        deadline = time.monotonic() + config["timeout_s"]
        while True:
            try:
                with urllib.request.urlopen(config["ready_url"] or config["base_url"], timeout=1):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    print("service failed readiness probe", flush=True)
                    return 1
                time.sleep(0.1)
        return subprocess.call(config["argv"])
    finally:
        stop_service(process)


if __name__ == "__main__":
    sys.exit(main())
