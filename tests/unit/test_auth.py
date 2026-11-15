import pytest

from sca.api.auth import StaticTokenAuthenticator
from sca.core.errors import AuthError


async def test_authenticate():
    auth = StaticTokenAuthenticator({"secret": "tenant"})
    principal = await auth.authenticate("secret")
    assert principal.tenant_id == "tenant"
    assert principal.subject
    for token in (None, "", "wrong"):
        with pytest.raises(AuthError):
            await auth.authenticate(token)


@pytest.fixture
def oidc(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from sca.api.auth import OidcAuthenticator

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = OidcAuthenticator("http://issuer", "sca", "http://issuer/keys")
    jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    monkeypatch.setattr(
        auth.jwks, "fetch_data", lambda: {"keys": [{**jwk._jwk_data, "kid": "test"}]}
    )
    return auth, key


async def test_oidc_valid(oidc):
    import time

    import jwt

    auth, key = oidc
    token = jwt.encode(
        {
            "iss": "http://issuer",
            "aud": "sca",
            "exp": time.time() + 60,
            "sub": "user",
            "tenant": "a",
        },
        key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    principal = await auth.authenticate(token)
    assert (principal.tenant_id, principal.subject) == ("a", "user")


@pytest.mark.parametrize(
    "invalid", ["issuer", "audience", "signature", "expired", "tenant", "none", "hs256"]
)
async def test_oidc_invalid(oidc, invalid):
    import time

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    auth, key = oidc
    claims = {
        "iss": "http://issuer",
        "aud": "sca",
        "exp": time.time() + 60,
        "sub": "user",
        "tenant": "a",
    }
    algorithm = "RS256"
    if invalid == "issuer":
        claims["iss"] = "other"
    elif invalid == "audience":
        claims["aud"] = "other"
    elif invalid == "signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif invalid == "expired":
        claims["exp"] = time.time() - 60
    elif invalid == "tenant":
        del claims["tenant"]
    elif invalid == "none":
        algorithm, key = "none", None
    elif invalid == "hs256":
        algorithm = "HS256"
        key = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    if invalid == "hs256":
        import base64
        import hashlib
        import hmac
        import json

        def encode(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        body = (
            encode(json.dumps({"alg": "HS256"}).encode())
            + b"."
            + encode(json.dumps(claims).encode())
        )
        token = (body + b"." + encode(hmac.new(key, body, hashlib.sha256).digest())).decode()
    else:
        token = jwt.encode(claims, key, algorithm=algorithm, headers={"kid": "test"})
    with pytest.raises(AuthError):
        await auth.authenticate(token)


async def test_chain_order():
    from sca.api.auth import ChainAuthenticator

    first = StaticTokenAuthenticator({"same": "first"})
    second = StaticTokenAuthenticator({"same": "second", "other": "second"})
    auth = ChainAuthenticator([first, second])
    assert (await auth.authenticate("same")).tenant_id == "first"
    assert (await auth.authenticate("other")).tenant_id == "second"
    with pytest.raises(AuthError):
        await auth.authenticate("missing")


async def test_oidc_fetch_off_event_loop(oidc, monkeypatch):
    import threading
    import time

    import jwt

    auth, key = oidc
    main_thread = threading.get_ident()
    original = auth.jwks.fetch_data

    def fetch():
        assert threading.get_ident() != main_thread
        return original()

    monkeypatch.setattr(auth.jwks, "fetch_data", fetch)
    token = jwt.encode(
        {
            "iss": "http://issuer",
            "aud": "sca",
            "exp": time.time() + 60,
            "sub": "user",
            "tenant": "a",
        },
        key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    assert (await auth.authenticate(token)).tenant_id == "a"


@pytest.mark.parametrize(
    "roles,expected",
    [
        (None, "member"),
        ("viewer", "viewer"),
        ("admin", "admin"),
        (["viewer", "admin", "member"], "admin"),
        (["unknown"], "member"),
        ([], "member"),
        (42, "member"),
    ],
)
async def test_oidc_roles(oidc, roles, expected):
    import time

    import jwt

    auth, key = oidc
    claims = {
        "iss": "http://issuer",
        "aud": "sca",
        "exp": time.time() + 60,
        "sub": "user",
        "tenant": "a",
    }
    if roles is not None:
        claims["roles"] = roles
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
    assert (await auth.authenticate(token)).role == expected
    auth.role_claim = "custom"
    auth.default_role = "viewer"
    assert (await auth.authenticate(token)).role == "viewer"


@pytest.mark.parametrize(
    "entry,role",
    [
        ("tenant", "admin"),
        ("tenant:viewer", "viewer"),
        ("tenant:member", "member"),
        ("tenant:admin", "admin"),
    ],
)
async def test_static_roles(entry, role):
    from sca.config import Settings

    auth = StaticTokenAuthenticator(Settings(api_tokens="secret:" + entry).token_table())
    principal = await auth.authenticate("secret")
    assert principal.tenant_id == "tenant"
    assert principal.role == role
