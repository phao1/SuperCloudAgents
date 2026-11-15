from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path

from sca.core.errors import SandboxError
from sca.core.models import ExecEvent, SandboxSpec
from sca.sandbox._exec import stream_exec
from sca.sandbox._transfer import transfer
from sca.sandbox.egress import SQUID_IMAGE, SQUID_PORT, render_squid_conf


class DockerSandboxProvider:
    def __init__(
        self,
        docker_bin: str = "docker",
        *,
        egress_allowlist: list[str] | None = None,
        egress_image: str = SQUID_IMAGE,
        name_prefix: str = "sca",
        peers: list[str] | None = None,
    ):
        # Containers restricted sandboxes may reach directly (the model gateway).
        self.peers = peers or []
        self.docker_bin = docker_bin
        self.egress_allowlist = egress_allowlist or []
        self.egress_image = egress_image
        # Restricted sandboxes join this network, which has no route out; the proxy
        # container is attached to it and to the default bridge.
        self.network_name = f"{name_prefix}-sandbox-net"
        self.proxy_name = f"{name_prefix}-egress"
        self._egress_lock = asyncio.Lock()

    async def ensure_egress(self) -> str:
        """Make sure the internal network and the egress proxy exist. Returns the proxy URL."""
        allow = ",".join(self.egress_allowlist)
        async with self._egress_lock:
            code, _, _ = await self._cli("network", "inspect", self.network_name, check=False)
            if code:
                code, _, err = await self._cli(
                    "network", "create", "--internal", self.network_name, check=False
                )
                if code and "already exists" not in err:
                    raise SandboxError(err)
            code, out, _ = await self._cli(
                "inspect",
                "--format",
                '{{.State.Running}} {{index .Config.Labels "sca.egress_allow"}}',
                self.proxy_name,
                check=False,
            )
            if code or out.strip() != f"true {allow}":
                await self._cli("rm", "-f", self.proxy_name, check=False)
                # The configuration travels in an env var so nothing has to be written
                # on the host; -N keeps Squid in the foreground.
                await self._cli(
                    "run", "-d", "--name", self.proxy_name,
                    "--restart", "unless-stopped",
                    # Squid drops to this user anyway; starting as it lets it log to stdout.
                    "--user", "proxy",
                    "--memory", "256m",
                    "--label", f"sca.egress_allow={allow}",
                    "--env", f"SQUID_CONF={render_squid_conf(self.egress_allowlist)}",
                    "--entrypoint", "sh",
                    self.egress_image,
                    "-c", 'printf "%s" "$SQUID_CONF" > /tmp/squid.conf && exec squid -N -f /tmp/squid.conf',
                )  # fmt: skip
                await self._cli("network", "connect", self.network_name, self.proxy_name)
        for peer in self.peers:
            code, _, err = await self._cli(
                "network", "connect", self.network_name, peer, check=False
            )
            if code and "already exists" not in err:
                raise SandboxError(err)
        # By address, not by name: gVisor does not support Docker's embedded DNS, so
        # container names do not resolve inside a runsc sandbox.
        return f"http://{await self.address_of(self.proxy_name)}:{SQUID_PORT}"

    async def address_of(self, container: str) -> str:
        """The container's address on the sandbox network."""
        _, out, _ = await self._cli(
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{self.network_name}").IPAddress}}}}',
            container,
        )
        if not out.strip():
            raise SandboxError(f"{container} is not attached to {self.network_name}")
        return out.strip()

    async def _cli(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError(str(exc)) from exc
        out, err = await proc.communicate()
        code = proc.returncode
        if check and code:
            raise SandboxError(err.decode(errors="replace"))
        return code or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def create(self, name: str, spec: SandboxSpec) -> str:
        sandbox_id = f"sca-{name}"
        code, out, err = await self._cli(
            "inspect", "--format", "{{.State.Running}}", sandbox_id, check=False
        )
        if not code and out.strip() == "true":
            return sandbox_id
        if code and "no such" not in err.lower():
            raise SandboxError(err)
        if not code:
            await self.destroy(sandbox_id)
        args = [
            "run",
            "-d",
            "--name",
            sandbox_id,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--init",
            "--cpus",
            str(spec.cpus),
            "--memory",
            f"{spec.memory_mb}m",
            "--workdir",
            spec.workdir,
        ]
        env = dict(spec.env)
        if spec.network == "none":
            args += ["--network", "none"]
        elif spec.network == "restricted":
            proxy = await self.ensure_egress()
            args += ["--network", self.network_name]
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.setdefault(key, proxy)
            direct = ",".join(
                ["localhost", "127.0.0.1", *[await self.address_of(p) for p in self.peers]]
            )
            env.setdefault("NO_PROXY", direct)
            env.setdefault("no_proxy", direct)
        if spec.runtime:
            args += ["--runtime", spec.runtime]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        for mount in spec.mounts:
            source = Path(mount.source).resolve()
            if source.name in {"docker.sock", "docker.socket"} or mount.target.endswith(
                ("/docker.sock", "/docker.socket")
            ):
                raise SandboxError("Mounting the Docker socket is forbidden")
            if source.exists() and stat.S_ISSOCK(source.stat().st_mode):
                raise SandboxError("Mounting host sockets is forbidden")
            if not source.exists():
                source.mkdir(parents=True)
            args += ["--volume", f"{source}:{mount.target}" + (":ro" if mount.read_only else "")]
        await self._cli(*args, spec.image, "sleep", "infinity")
        return sandbox_id

    async def list(self, labels: dict[str, str]) -> list[str]:
        filters = [
            arg
            for key, value in labels.items()
            for arg in ("--filter", "label=" + key + ("=" + value if value else ""))
        ]
        _, out, _ = await self._cli("ps", "-a", *filters, "--format", "{{.Names}}")
        return out.split()

    async def exists(self, sandbox_id: str) -> bool:
        code, _, err = await self._cli("inspect", sandbox_id, check=False)
        if code and "no such" not in err.lower():
            raise SandboxError(err)
        return code == 0

    async def destroy(self, sandbox_id: str) -> None:
        code, _, err = await self._cli("rm", "-f", sandbox_id, check=False)
        if code and "no such" not in err.lower():
            raise SandboxError(err)

    def exec(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecEvent]:
        def command(wrapper: str, marker: str) -> list[str]:
            args = [self.docker_bin, "exec", "-i"]
            if workdir:
                args += ["--workdir", workdir]
            for key, value in (env or {}).items():
                args += ["--env", f"{key}={value}"]
            return [*args, sandbox_id, "python3", "-c", wrapper, marker, *argv]

        async def cleanup(script: str, marker: str) -> None:
            await self._cli("exec", sandbox_id, "python3", "-c", script, marker, check=False)

        return stream_exec(command, cleanup, stdin=stdin, timeout_s=timeout_s)

    async def upload(self, sandbox_id: str, target_dir: str, archive: AsyncIterator[bytes]) -> None:
        # docker cp extracts as root; fix ownership using the configured container user.
        _, user, _ = await self._cli("inspect", "--format", "{{.Config.User}}", sandbox_id)
        await self._cli("exec", "--user", "0", sandbox_id, "mkdir", "-p", "--", target_dir)
        async for _ in transfer(
            [self.docker_bin, "cp", "-", f"{sandbox_id}:{target_dir}"], archive
        ):
            pass
        await self._cli(
            "exec", "--user", "0", sandbox_id, "chown", "-R", "--", user.strip(), target_dir
        )

    def download(self, sandbox_id: str, path: str) -> AsyncIterator[bytes]:
        return transfer([self.docker_bin, "cp", f"{sandbox_id}:{path}", "-"])
