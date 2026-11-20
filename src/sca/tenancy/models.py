from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sca.core.errors import ScaError

TenantSlug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Email = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
)]
PlanName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
MemberRole = Literal["owner", "admin", "member", "viewer"]
TenantStatus = Literal["active", "suspended"]


class TenantSuspendedError(ScaError):
    def __init__(self) -> None:
        super().__init__("租户已暂停，无法提交新任务")


class TenantNameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName


class TenantCreate(TenantNameUpdate):
    id: TenantSlug
    plan: PlanName = "free"
    owner_email: Email | None = None


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName | None = None
    plan: PlanName | None = None


class MemberInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    role: Literal["admin", "member", "viewer"] = Field(default="member")


class MemberRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "member", "viewer"]


class PlatformMemberInvite(MemberInvite):
    role: MemberRole = "owner"
