"""Use the restricted Docker driver on an isolated network for MCP tasks."""

import contextlib
import hashlib
from urllib.parse import urlsplit

from sca.core.errors import SandboxError
from sca.core.interfaces import SandboxProvider
from sca.core.models import SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider

_SUFFIX = "-mcp.scope"
_LABEL = "sca.mcp_egress"


class McpSandboxSpec(SandboxSpec):
    mcp_hosts: list[str]


class ToolSandboxProvider:
    def __init__(self, provider: DockerSandboxProvider) -> None:
        self.provider = provider

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def _scoped(self, sandbox_id: str, hosts: list[str]) -> DockerSandboxProvider:
        return DockerSandboxProvider(
            self.provider.docker_bin,
            name_prefix=sandbox_id,
            egress_allowlist=[*self.provider.egress_allowlist, *hosts],
            egress_image=self.provider.egress_image,
            peers=self.provider.peers,
            egress_ca_dir=self.provider.egress_ca_dir,
        )

    async def create(self, name: str, spec: SandboxSpec) -> str:
        if not isinstance(spec, McpSandboxSpec):
            return await self.provider.create(name, spec)
        if spec.network != "restricted":
            raise SandboxError("MCP sandbox requires a restricted network")
        scoped_name = f"{name}{_SUFFIX}"
        sandbox_id = f"sca-{scoped_name}"
        scope = self._scoped(sandbox_id, spec.mcp_hosts)
        fingerprint = hashlib.sha256("\n".join(scope.egress_allowlist).encode()).hexdigest()
        code, existing, _ = await self.provider._cli(
            "inspect", "--format", '{{index .Config.Labels "sca.mcp_egress"}}', sandbox_id, check=False,
        )
        if not code and existing.strip() != fingerprint:
            await self.destroy(sandbox_id)
        try:
            return await scope.create(scoped_name, spec.model_copy(
                update={"labels": {**spec.labels, _LABEL: fingerprint}},
            ))
        except BaseException:
            with contextlib.suppress(SandboxError):
                await self.destroy(sandbox_id)
            raise

    async def destroy(self, sandbox_id: str) -> None:
        await self.provider.destroy(sandbox_id)
        if sandbox_id.endswith(_SUFFIX):
            scope = self._scoped(sandbox_id, [])
            await self.provider.destroy(scope.proxy_name)
            for peer in self.provider.peers:
                await self.provider._cli("network", "disconnect", "-f", scope.network_name, peer,
                                         check=False)
            code, _, err = await self.provider._cli("network", "rm", scope.network_name, check=False)
            if code and "not found" not in err.lower() and "no such" not in err.lower():
                raise SandboxError("Unable to remove the MCP sandbox network")

    async def mcp_model_url(self, sandbox_id: str, url: str, peer: str) -> str:
        if not sandbox_id.endswith(_SUFFIX) or peer not in self.provider.peers:
            return url
        address = await self._scoped(sandbox_id, []).address_of(peer)
        parts = urlsplit(url)
        return parts._replace(netloc=f"{address}:{parts.port}" if parts.port else address).geturl()


def with_tool_egress(sandbox: SandboxProvider) -> SandboxProvider:
    return ToolSandboxProvider(sandbox) if isinstance(sandbox, DockerSandboxProvider) else sandbox
