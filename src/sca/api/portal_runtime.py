"""Inspect and switch a local launcher-managed Portal runtime."""

import asyncio
import os
import platform
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from sca.api.authz import authorize, local_portal_host
from sca.core.models import Principal
from sca.runtime_mode import RuntimeMode, preferred_mode
from sca.runtime_switch import SwitchStatus, begin_switch, read_switch

router = APIRouter(prefix="/v1/portal/runtime")


@dataclass(frozen=True)
class PortalRuntime:
    root: Path
    mode: RuntimeMode


async def runtime_admin(
    request: Request, user: Annotated[Principal, Depends(authorize("quota"))],
) -> PortalRuntime:
    if user.tenant_id != "eval" or user.scopes:
        raise HTTPException(403, "运行模式设置需要 eval 租户管理员权限。")
    if local_portal_host(request) is None:
        raise HTTPException(403, "运行模式设置仅允许从 Portal 所在的本机访问。")
    config = os.environ.get("SCA_PORTAL_LOCAL_CONFIG")
    mode = os.environ.get("SCA_RUNTIME_MODE")
    if (not config or mode not in ("docker", "source")
            or (mode == "docker" and not os.environ.get("SCA_DOCKER_BIN"))):
        raise HTTPException(403, "请通过本机 Portal 启动脚本运行 up。")
    root = Path(config).absolute().parent
    if not (root / "scripts/portal_switch.py").is_file():
        raise HTTPException(403, "找不到本机 Portal 切换脚本。")
    return PortalRuntime(root, cast(RuntimeMode, mode))


LocalRuntime = Annotated[PortalRuntime, Depends(runtime_admin)]

_DOCKER_ERRORS = {
    2: "找不到 Docker CLI，请先安装 Docker。",
    3: "无法连接 Docker 服务，请启动 Docker 或检查 docker 组权限。",
    4: "Docker Compose 不可用，请安装 Compose 插件。",
    5: "Docker 模式需要 GNU timeout（macOS 可安装 coreutils）。",
}


async def docker_unavailable(root: Path) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "bash", str(root / "scripts/portal_demo.sh"), "probe-docker", cwd=root,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return "无法运行 Docker 模式启动脚本。"
    try:
        code = await asyncio.wait_for(process.wait(), 25)
    except TimeoutError:
        return "Docker 服务检测超时，请检查 Docker 状态。"
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    return _DOCKER_ERRORS.get(code, "无法检测 Docker 状态。") if code else None


class ModeAvailability(BaseModel):
    mode: RuntimeMode
    available: bool
    reason: str | None = None


async def mode_availability(root: Path, system: str, mode: RuntimeMode) -> ModeAvailability:
    reason = None
    if mode == "docker":
        if system == "Windows":
            reason = "Windows 版仅支持源码模式"
        elif system in ("Linux", "Darwin"):
            reason = await docker_unavailable(root)
        else:
            reason = "Docker 模式支持 Linux 和 macOS。"
    elif system not in ("Linux", "Darwin", "Windows"):
        reason = "源码模式支持 Linux、macOS 和 Windows。"
    return ModeAvailability(mode=mode, available=reason is None, reason=reason)


class RuntimeInfo(BaseModel):
    current_mode: RuntimeMode
    platform: str
    modes: list[ModeAvailability]
    preferred_mode: RuntimeMode | None
    switch: SwitchStatus | None


class RuntimeSwitchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: RuntimeMode
    carry_history: StrictBool = True


class RuntimeSwitchAccepted(BaseModel):
    id: UUID


@router.get("")
async def get_runtime(runtime: LocalRuntime, response: Response) -> RuntimeInfo:
    system = platform.system()
    modes = [await mode_availability(runtime.root, system, mode) for mode in ("source", "docker")]
    data = runtime.root / ".data"
    try:
        record = read_switch(data)
    except OSError:
        raise HTTPException(503, "无法读取运行模式切换记录。") from None
    response.headers["Cache-Control"] = "no-store"
    return RuntimeInfo(current_mode=runtime.mode, platform=system, modes=modes,
                       preferred_mode=preferred_mode(data), switch=record)


@router.post("/switch", status_code=202)
async def switch_runtime(body: RuntimeSwitchInput, runtime: LocalRuntime) -> RuntimeSwitchAccepted:
    if body.mode == runtime.mode:
        raise HTTPException(409, "Portal 已在所选模式运行。")
    choice = await mode_availability(runtime.root, platform.system(), body.mode)
    if not choice.available:
        raise HTTPException(409, choice.reason)
    try:
        switch_id = begin_switch(runtime.root, runtime.mode, body.mode, body.carry_history)
    except BlockingIOError:
        raise HTTPException(409, "运行模式正在切换，请等待当前切换完成。") from None
    except OSError:
        raise HTTPException(503, "无法启动运行模式切换。") from None
    return RuntimeSwitchAccepted(id=switch_id)
