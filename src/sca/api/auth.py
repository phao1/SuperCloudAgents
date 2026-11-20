from sca.core.errors import AuthError
from sca.core.interfaces import Authenticator
from sca.core.models import Principal
from sca.db.repo import Store


class StaticTokenAuthenticator:
    def __init__(self, table: dict[str, str], store: Store | None = None) -> None:
        self.table = dict(table)
        self.store = store

    async def authenticate(self, token: str | None) -> Principal:
        if not token or token not in self.table:
            raise AuthError("Invalid or missing bearer token")
        tenant, _, role = self.table[token].partition(":")
        if role and role not in {"viewer", "member", "admin"}:
            raise AuthError("Invalid static token role")
        if self.store is not None and await self.store.tenant_deleted(tenant):
            raise AuthError("Tenant has been deleted")
        return Principal(tenant_id=tenant, subject="static-token", role=role or "admin")


class OidcAuthenticator:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        tenant_claim: str = "tenant",
        role_claim: str = "roles",
        default_role: str = "member",
        store: Store | None = None,
    ) -> None:
        from jwt import PyJWKClient

        self.issuer = issuer
        self.audience = audience
        self.tenant_claim = tenant_claim
        self.role_claim = role_claim
        self.default_role = default_role
        self.store = store
        self.jwks = PyJWKClient(jwks_url)

    async def authenticate(self, token: str | None) -> Principal:
        import asyncio

        import jwt

        if not token:
            raise AuthError("Invalid or missing bearer token")

        def verify() -> dict:
            try:
                if jwt.get_unverified_header(token).get("alg") not in {"RS256", "ES256"}:
                    raise AuthError("Unsupported signing algorithm")
                key = self.jwks.get_signing_key_from_jwt(token)
                required = ["exp", "iss", "aud", "sub"]
                if self.store is None:
                    required.append(self.tenant_claim)
                return jwt.decode(
                    token,
                    key.key,
                    algorithms=["RS256", "ES256"],
                    issuer=self.issuer,
                    audience=self.audience,
                    options={"require": required},
                )
            except jwt.PyJWTError as exc:
                raise AuthError("Invalid bearer token") from exc

        claims = await asyncio.to_thread(verify)
        tenant, subject = claims.get(self.tenant_claim), claims["sub"]
        if not isinstance(subject, str) or not subject:
            raise AuthError("Missing tenant or subject")
        if self.tenant_claim in claims and (not isinstance(tenant, str) or not tenant):
            raise AuthError("Invalid tenant claim")
        roles = claims.get(self.role_claim, [])
        if isinstance(roles, str):
            roles = [roles]
        known = [r for r in ("viewer", "member", "admin") if isinstance(roles, list) and r in roles]
        role = known[-1] if known else self.default_role

        if self.store is not None:
            tenant, member_role = await self._membership(tenant, subject, claims)
            role = ("admin" if member_role == "owner" else member_role) or role
        if tenant is None:
            raise AuthError("Missing tenant claim or membership")
        return Principal(tenant_id=tenant, subject=subject, role=role)

    async def _membership(
        self, tenant: str | None, subject: str, claims: dict,
    ) -> tuple[str, str | None]:
        assert self.store is not None
        email = claims.get("email")
        email = (email.strip().lower() if isinstance(email, str)
                 and claims.get("email_verified") is True else None)
        if tenant is None or self.tenant_claim == "email":
            matches = await self.store.oidc_member_tenants(subject, email)
            if len(matches) > 1:
                raise AuthError("Tenant claim required for multiple memberships")
            if matches:
                tenant = matches[0]
        if tenant is None:
            raise AuthError("Missing tenant claim or membership")
        required, role = await self.store.for_tenant(tenant).resolve_oidc_membership(subject, email)
        if required and role is None:
            raise AuthError("OIDC identity is not a member of this tenant")
        return tenant, role


class ChainAuthenticator:
    def __init__(self, authenticators: list["Authenticator"]) -> None:
        self.authenticators = authenticators

    async def authenticate(self, token: str | None) -> Principal:
        for authenticator in self.authenticators:
            try:
                return await authenticator.authenticate(token)
            except AuthError:
                continue
        raise AuthError("Invalid or missing bearer token")


class DatabaseTokenAuthenticator:
    def __init__(self, store: "Store") -> None:
        self.store = store

    async def authenticate(self, token: str | None) -> Principal:
        import hashlib

        if not token or not token.startswith("sca_"):
            raise AuthError("Invalid bearer token")
        row = await self.store.lookup_api_token(hashlib.sha256(token.encode()).hexdigest())
        if row is None or await self.store.tenant_deleted(row["tenant_id"]):
            raise AuthError("Invalid bearer token")
        return Principal(
            tenant_id=row["tenant_id"],
            subject=str(row["id"]),
            role=row["role"] or "member",
            scopes=row["scopes"],
            repo_url=row["repo_url"],
        )
