"""Egress allowlist for restricted sandboxes, enforced by Squid.

Sandboxes sit on a network with no route out; a Squid container is their only exit.
This module only renders Squid's configuration.
"""

from __future__ import annotations

import re

SQUID_IMAGE = "ubuntu/squid:latest"
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
        domains.append("." + host[2:] if host.startswith("*.") else host)
    # Squid rejects a list holding both `.example.com` and `example.com`.
    wild = {d for d in domains if d.startswith(".")}
    return sorted(d for d in set(domains) if d in wild or "." + d not in wild)


def render_squid_conf(allowlist: list[str]) -> str:
    domains = squid_domains(allowlist)
    allow = (
        f"acl allowed dstdomain {' '.join(domains)}\nhttp_access allow allowed\n" if domains else ""
    )
    return (
        f"http_port {SQUID_PORT}\n"
        "acl Safe_ports port 80 443\n"
        "acl SSL_ports port 443\n"
        "acl CONNECT method CONNECT\n"
        f"acl private dst {_PRIVATE}\n"
        "http_access deny !Safe_ports\n"
        "http_access deny CONNECT !SSL_ports\n"
        "http_access deny private\n"
        f"{allow}"
        "http_access deny all\n"
        "cache deny all\n"
        "access_log stdio:/dev/stdout\n"
        "cache_log /dev/stderr\n"
        "pid_filename none\n"
        "shutdown_lifetime 1 seconds\n"
    )
