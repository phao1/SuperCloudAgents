"""Opt-in real Docker freezer benchmark; no builds or dependency installation."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4


def docker(binary: str, *args: str) -> str:
    return subprocess.run([binary, *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout


def cpu() -> tuple[int, int]:
    values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def measure(binary: str, names: list[str], seconds: float) -> dict:
    before, idle_before = cpu()
    started = time.monotonic()
    samples = []
    while time.monotonic() - started < seconds:
        samples.append([json.loads(line) for line in docker(
            binary, "stats", "--no-stream", "--format", "{{json .}}", *names).splitlines()])
        time.sleep(min(1, seconds))
    after, idle_after = cpu()
    return {"elapsed_s": time.monotonic() - started,
            "host_cpu_percent": 100 * (1 - (idle_after - idle_before) / max(1, after - before)),
            "docker_stats": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--containers", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--image", default="python:3.12-alpine")
    parser.add_argument("--docker-bin", default="docker")
    args = parser.parse_args()
    if not 1 <= args.containers <= 32 or not 1 <= args.seconds <= 300:
        parser.error("containers must be 1..32 and seconds 1..300")
    names = [f"sca-suspend-bench-{uuid4().hex[:12]}" for _ in range(args.containers)]
    report = {"containers": args.containers, "image": args.image}
    try:
        for name in names:
            docker(args.docker_bin, "run", "-d", "--name", name, "--network", "none",
                   "--memory", "32m", "--cpus", "0.1", "--pids-limit", "16", args.image,
                   "python3", "-c", "import time; data=bytearray(4*1024*1024)\nwhile True: time.sleep(.05)")
        time.sleep(2)
        report["running"] = measure(args.docker_bin, names, args.seconds)
        for name in names:
            docker(args.docker_bin, "pause", name)
        report["paused"] = measure(args.docker_bin, names, args.seconds)
        report["interpretation"] = (
            "冻结预期减少 CPU 与调度唤醒，不释放容器内存。主机 CPU 受其他进程影响；"
            "请比较 docker_stats 的 CPUPerc 与 MemUsage，多次运行后再下结论。")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for name in names:
            subprocess.run([args.docker_bin, "rm", "-f", name], capture_output=True, timeout=30, check=False)


if __name__ == "__main__":
    main()
