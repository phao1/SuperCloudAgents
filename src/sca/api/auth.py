from sca.core.errors import AuthError
from sca.core.interfaces import Authenticator
from sca.core.models import Principal
from sca.db.repo import Store


class StaticTokenAuthenticator:
    def __init__(self, table: dict[str, str]) -> None:
        self.table = dict(table)

    async def authenticate(self, token: str | None) -> Principal:
        if not token or token not in self.table:
            raise AuthError("Invalid or missing bearer token")
        tenant, _, role = self.table[token].partition(":")
        if role and role not in {"viewer", "member", "admin"}:
            raise AuthError("Invalid static token role")
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
    ) -> None:
        from jwt import PyJWKClient

        self.issuer = issuer
        self.audience = audience
        self.tenant_claim = tenant_claim
        self.role_claim = role_claim
        self.default_role = default_role
        self.jwks = PyJWKClient(jwks_url)

    async def authenticate(self, token: str | None) -> Principal:
        import asyncio

        import jwt

        if not token:
            raise AuthError("Invalid or missing bearer token")

        def verify() -> Principal:
            try:
                if jwt.get_unverified_header(token).get("alg") not in {"RS256", "ES256"}:
                    raise AuthError("Unsupported signing algorithm")
                key = self.jwks.get_signing_key_from_jwt(token)
                claims = jwt.decode(
                    token,
                    key.key,
                    algorithms=["RS256", "ES256"],
                    issuer=self.issuer,
                    audience=self.audience,
                    options={"require": ["exp", "iss", "aud", "sub", self.tenant_claim]},
                )
                tenant = claims[self.tenant_claim]
                if not isinstance(tenant, str) or not tenant or not claims["sub"]:
                    raise AuthError("Missing tenant or subject")
                roles = claims.get(self.role_claim, [])
                if isinstance(roles, str):
                    roles = [roles]
                known = [
                    r
                    for r in ("viewer", "member", "admin")
                    if isinstance(roles, list) and r in roles
                ]
                return Principal(
                    tenant_id=tenant,
                    subject=claims["sub"],
                    role=known[-1] if known else self.default_role,
                )
            except jwt.PyJWTError as exc:
                raise AuthError("Invalid bearer token") from exc

        return await asyncio.to_thread(verify)


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
        if row is None:
            raise AuthError("Invalid bearer token")
        return Principal(
            tenant_id=row["tenant_id"],
            subject=str(row["id"]),
            role=row["role"] or "member",
            scopes=row["scopes"],
            repo_url=row["repo_url"],
        )
