"""Sandbox-only service lifetime and readiness wrapper."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


def main() -> int:
    with open(sys.argv[1]) as stream:
        config = json.load(stream)
    process = subprocess.Popen(["bash", "-lc", config["start_command"]], start_new_session=True)
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
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


if __name__ == "__main__":
    sys.exit(main())
