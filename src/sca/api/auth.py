from sca.core.errors import AuthError
from sca.core.interfaces import Authenticator
from sca.core.models import Principal


class StaticTokenAuthenticator:
    def __init__(self, table: dict[str, str]) -> None:
        self.table = dict(table)

    async def authenticate(self, token: str | None) -> Principal:
        if not token or token not in self.table:
            raise AuthError("Invalid or missing bearer token")
        return Principal(tenant_id=self.table[token], subject="static-token")


class OidcAuthenticator:
    def __init__(
        self, issuer: str, audience: str, jwks_url: str, tenant_claim: str = "tenant"
    ) -> None:
        from jwt import PyJWKClient

        self.issuer = issuer
        self.audience = audience
        self.tenant_claim = tenant_claim
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
                return Principal(tenant_id=tenant, subject=claims["sub"])
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
