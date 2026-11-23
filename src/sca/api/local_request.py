"""Local request checks shared by the live API and the standalone archive viewer."""

from __future__ import annotations

import re
from collections.abc import Sequence
from ipaddress import IPv6Address, ip_address

_PROXY_HEADERS = {"forwarded", "via", "x-real-ip"}
_LOOPBACK_AUTHORITY = re.compile(
    r"(?:127\.0\.0\.1|localhost|\[::1\])(?::0*([0-9]{1,5}))?", re.ASCII | re.IGNORECASE,
)


def is_loopback(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def unproxied_host(headers: Sequence[tuple[str, str]]) -> str | None:
    hosts = []
    for name, value in headers:
        name = name.lower()
        if name in _PROXY_HEADERS or name.startswith("x-forwarded-"):
            return None
        if name == "host":
            hosts.append(value)
    return hosts[0] if len(hosts) == 1 else None


def _loopback_authority(value: str) -> bool:
    match = _LOOPBACK_AUTHORITY.fullmatch(value)
    return match is not None and (match[1] is None or int(match[1]) <= 65535)


def local_guest_request(peer: str, headers: Sequence[tuple[str, str]]) -> bool:
    if not is_loopback(peer):
        return False
    host = unproxied_host(headers)
    if host is None or not _loopback_authority(host.strip(" \t")):
        return False

    origins = []
    for name, value in headers:
        name = name.lower()
        if name == "origin":
            origins.append(value.strip(" \t"))
        elif name == "sec-fetch-site":
            sites = (site.strip(" \t").lower() for site in value.split(","))
            if any(site in {"cross-site", "same-site"} for site in sites):
                return False
    if not origins:
        return True
    if len(origins) != 1:
        return False
    scheme, _, authority = origins[0].partition("://")
    return scheme.lower() in {"http", "https"} and _loopback_authority(authority)
