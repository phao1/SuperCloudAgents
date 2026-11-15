"""B9: how long a sandbox takes from `create` to the first successful `exec`.

    python scripts/bench_sandbox_start.py --driver k8s --runs 20 [--runtime runsc]

Prints p50/p95 per (network mode) and a JSON line for the design document. The image must
already be present on the nodes: this measures start-up, not image pulls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from uuid import uuid4

from sca.core.models import ExecExit, SandboxSpec


def provider(driver: str, namespace: str, node: str | None, pool: int = 0):
    if driver == "docker":
        from sca.sandbox.docker import DockerSandboxProvider

        return DockerSandboxProvider(egress_allowlist=["pypi.org"], name_prefix="bench")
    from sca.sandbox.k8s import K8sSandboxProvider

    return K8sSandboxProvider(
        str(Path.home() / ".local/bin/kubectl"),
        context="kind-sca",
        pool_size=pool,
        namespace=namespace,
        egress_allowlist=["pypi.org"],
        node_selector={"kubernetes.io/hostname": node} if node else None,
    )


async def once(p, network: str, runtime: str | None) -> float:
    name = f"bench-{uuid4().hex[:10]}"
    spec = SandboxSpec(
        image="sca-sandbox:latest",
        network=network,
        runtime=runtime,
        cpus=0.5,
        memory_mb=256,
        labels={"sca.state_transport": "sync"},
    )
    if getattr(p, "pool_size", 0):
        await p.replenish(spec, p.pool_size)
    start = time.monotonic()
    sid = await p.create(name, spec)
    try:
        async for event in p.exec(sid, ["true"]):
            if isinstance(event, ExecExit) and event.exit_code != 0:
                raise RuntimeError("exec failed")
        return time.monotonic() - start
    finally:
        await p.destroy(sid)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", choices=["docker", "k8s"], default="k8s")
    ap.add_argument("--pool", type=int, default=0)
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--runtime", default=None)
    ap.add_argument("--node", default=None)
    args = ap.parse_args()
    namespace = f"sca-bench-{uuid4().hex[:6]}"
    p = provider(args.driver, namespace, args.node, args.pool)
    results = {}
    try:
        for network in ("none", "restricted"):
            await once(p, network, args.runtime)  # warm-up: namespace, egress proxy
            times = [await once(p, network, args.runtime) for _ in range(args.runs)]
            results[network] = {
                "p50_s": round(statistics.median(times), 2),
                "p95_s": round(pct(times, 0.95), 2),
                "max_s": round(max(times), 2),
            }
            print(
                f"{args.driver} runtime={args.runtime or 'default'} network={network}: "
                f"{results[network]}",
                flush=True,
            )
    finally:
        if args.driver == "k8s":
            await p._cli("delete", "namespace", namespace, "--wait=false", check=False)
    print(
        json.dumps(
            {
                "driver": args.driver,
                "runtime": args.runtime,
                "runs": args.runs,
                "pool": args.pool,
                "results": results,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
