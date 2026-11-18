"""Egress allowlist for restricted sandboxes, enforced by Squid.

Sandboxes sit on a network with no route out; a Squid container is their only exit.
This module only renders Squid's configuration.
"""

from __future__ import annotations

import re
from ipaddress import ip_address
from pathlib import Path

SQUID_IMAGE = "sca-egress:tls-v1"
CA_TARGET = "/etc/sca-egress/ca-bundle.crt"
CA_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT",
          "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO")
SQUID_PORT = 3128

_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")

# Destinations that are never allowed, whatever the allowlist says: the proxy must not
# become a way into the host, the cluster or the cloud metadata service.
_PRIVATE = (
    "0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 "
    "192.168.0.0/16 ::1/128 fc00::/7 fe80::/10"
)


def squid_domains(allowlist: list[str]) -> list[str]:
    """Translate allowlist entries to Squid dstdomain values.

    `example.com` matches that host only; `*.example.com` matches its subdomains
    (and, as Squid's `.example.com` does, the bare domain too).
    """
    domains = []
    for entry in allowlist:
        host = entry.strip().lower().rstrip(".")
        if not _HOST.match(host):
            raise ValueError(f"invalid egress allowlist entry: {entry!r}")
        try:
            ip_address(host.removeprefix("*."))
        except ValueError:
            pass
        else:
            raise ValueError("IP literals are forbidden in the egress allowlist")
        domains.append("." + host[2:] if host.startswith("*.") else host)
    # Squid rejects a list holding both `.example.com` and `example.com`.
    wild = {d for d in domains if d.startswith(".")}
    return sorted(d for d in set(domains) if d in wild or "." + d not in wild)


def render_squid_conf(allowlist: list[str]) -> str:
    domains = squid_domains(allowlist)
    # Inside bumped TLS the Host header must also be on the allowlist, so a request to an
    # allowed CONNECT target cannot name another virtual host on the same CDN.
    hosts = "|".join(
        (r"([a-z0-9-]+\.)*" + re.escape(d[1:]) if d.startswith(".") else re.escape(d))
        for d in domains
    )
    allow = (
        f"acl allowed dstdomain -n {' '.join(domains)}\n"
        f"acl allowed_host req_header Host -i ^({hosts})\\.?(:[0-9]+)?$\n"
        "http_access deny !CONNECT !allowed_host\n"
        "http_access allow allowed\n" if domains else ""
    )
    return (
        f"http_port {SQUID_PORT} ssl-bump cert=/ca/ca.crt key=/ca/ca.key "
        "generate-host-certificates=on dynamic_cert_mem_cache_size=4MB\n"
        "sslcrtd_program /usr/lib/squid/security_file_certgen -s /tmp/ssl_db -M 4MB\n"
        "sslcrtd_children 2 startup=1 idle=1\n"
        "acl step1 at_step SslBump1\n"
        "external_acl_type raw_sni ttl=0 negative_ttl=0 children-max=2 cache=256 %ssl::>sni "
        "/usr/local/bin/check-sni\n"
        "acl has_sni external raw_sni\n"
        "ssl_bump peek step1\n"
        "ssl_bump terminate !has_sni\n"
        + (f"acl allowed_sni ssl::server_name --client-requested {' '.join(domains)}\n"
           "ssl_bump bump allowed_sni has_sni\n" if domains else "")
        + "ssl_bump terminate all\n"
        "acl Safe_ports port 80 443\n"
        "acl SSL_ports port 443\n"
        "acl CONNECT method CONNECT\n"
        'acl ip_literal url_regex -i ^[a-z]+://[0-9.]+[:/] ^[a-z]+://\\[ ^[0-9.]+: ^\\[\n'
        "http_access deny ip_literal\n"
        "host_verify_strict on\n"
        "sslproxy_cert_error deny all\n"
        f"acl private dst {_PRIVATE}\n"
        "http_access deny !Safe_ports\n"
        "http_access deny CONNECT !SSL_ports\n"
        "http_access deny private\n"
        f"{allow}"
        "http_access deny all\n"
        "cache deny all\n"
        "access_log none\n"
        "cache_log /dev/null\n"
        "pid_filename none\n"
        "shutdown_lifetime 1 seconds\n"
    )


async def prepare_ca(directory: Path) -> None:
    """Create a deployment CA once; publish only the public trust bundle to agents."""
    import asyncio

    await asyncio.to_thread(_prepare_ca, directory)


def _prepare_ca(directory: Path) -> None:
    import datetime
    import fcntl
    import os
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with (directory / "ca.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        key_path, cert_path = directory / "ca.key", directory / "ca.crt"
        if not key_path.exists() and not cert_path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SCA egress CA")])
            now = datetime.datetime.now(datetime.UTC)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(minutes=5))
                    .not_valid_after(now + datetime.timedelta(days=3650))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .sign(key, hashes.SHA256()))
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        if not key_path.exists() or not cert_path.exists():
            raise ValueError("Incomplete egress CA; restore deployment CA before starting")
        key_path.chmod(0o600)
        public = cert_path.read_bytes()
        defaults = ssl.get_default_verify_paths().cafile
        bundle = (Path(defaults).read_bytes() if defaults else b"") + b"\n" + public
        temporary = directory / "bundle.tmp"
        temporary.write_bytes(bundle)
        temporary.chmod(0o644)
        temporary.replace(directory / "ca-bundle.crt")
