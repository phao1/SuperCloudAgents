"""Identify supported repositories and resolve operator-configured API URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from sca.config import Settings

ProviderName = Literal["github", "gitea"]


@dataclass(frozen=True)
class Repository:
    provider: ProviderName
    host: str
    full_name: str
    clone_url: str
    api_url: str

    @property
    def web_url(self) -> str:
        return self.clone_url.removesuffix(".git")


def normalize_host(host: str) -> str:
    if not re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])(?::[0-9]+)?", host):
        raise ValueError("Invalid repository host")
    parsed = urlsplit(f"//{host}")
    if parsed.port is not None and parsed.port < 1:
        raise ValueError("Invalid repository port")
    return host.lower()


def normalize_repo_name(name: str) -> str:
    parts = name.split("/")
    if len(parts) != 2 or any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts
    ):
        raise ValueError("Expected repository name owner/repo")
    return name.lower()


def provider_for_host(settings: Settings, host: str) -> tuple[ProviderName, str]:
    if host == "github.com":
        return "github", str(settings.scm_github_api_url)
    for configured_host, api_url in settings.scm_gitea_hosts.items():
        if configured_host.lower() == host:
            return "gitea", str(api_url)
    raise ValueError("Unsupported SCM host")


def identify_repository(settings: Settings, url: str) -> Repository:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or parsed.query or parsed.fragment:
        raise ValueError("Expected an HTTP(S) repository clone URL")
    host = normalize_host(parsed.netloc)
    path = parsed.path.rstrip("/").removesuffix(".git")
    parts = path.removeprefix("/").split("/")
    if len(parts) < 2 or any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts
    ):
        raise ValueError("Invalid repository clone URL path")
    provider, api_url = provider_for_host(settings, host)
    if provider == "github" and (len(parts) != 2 or parsed.scheme != "https"):
        raise ValueError("Expected a GitHub HTTPS repository clone URL")
    clone_url = urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    return Repository(provider, host, "/".join(parts[-2:]), clone_url, api_url)
