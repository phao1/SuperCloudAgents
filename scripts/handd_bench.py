"""Opt-in Docker benchmark; run against an existing disposable sandbox."""
from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import time

from sca.sandbox.handd import DAEMON, HanddClient


def cpu() -> float:
    return sum(r.ru_utime + r.ru_stime for r in
               (resource.getrusage(resource.RUSAGE_SELF),
                resource.getrusage(resource.RUSAGE_CHILDREN)))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sandbox')
    parser.add_argument('--file', default='/tmp/sca-handd-bench.txt')
    parser.add_argument('--count', type=int, default=500)
    parser.add_argument('--docker', default='docker')
    args = parser.parse_args()
    if args.count < 1:
        parser.error('--count must be positive')
    client = HanddClient([args.docker, 'exec', '-i', args.sandbox,
                          'python3', '-u', '-c', DAEMON.read_text()])
    await client.start()
    try:
        for mode in ('docker', 'handd'):
            for concurrency in (1, 4):
                for command in (['true'], ['cat', args.file], ['rg', '1', args.file]):
                    semaphore = asyncio.Semaphore(concurrency)

                    async def run(semaphore=semaphore, mode=mode, command=command) -> float:
                        async with semaphore:
                            started = time.perf_counter()
                            if mode == 'docker':
                                proc = await asyncio.create_subprocess_exec(
                                    args.docker, 'exec', args.sandbox, *command,
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.DEVNULL)
                                code = await proc.wait()
                            else:
                                code = None
                                async for event in client.exec(command, timeout_s=30):
                                    if event.kind == 'exit':
                                        code = event.exit_code
                            if code != 0:
                                raise RuntimeError(f'benchmark command failed: {command[0]}')
                            return (time.perf_counter() - started) * 1000

                    before = cpu()
                    started = time.perf_counter()
                    samples = sorted(await asyncio.gather(*[run() for _ in range(args.count)]))
                    print(json.dumps({'mode': mode, 'concurrency': concurrency,
                        'argv': command, 'count': args.count,
                        'p50_ms': statistics.median(samples),
                        'p99_ms': samples[min(len(samples)-1, int(len(samples)*.99))],
                        'wall_s': time.perf_counter()-started, 'host_cpu_s': cpu()-before}),
                        flush=True)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
