"""Local GitHub App manifest setup. Run with .venv/bin/python scripts/github_app_setup.py."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data/github-app"
API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json"}


def save(directory: Path, name: str, value: str) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value)


def manifest(owner: str, repo: str, name: str) -> dict:
    if not re.fullmatch(r"[\w.-]+", owner) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Expected a GitHub user and owner/repo")
    return {
        "name": name,
        "url": f"https://github.com/{repo}",
        "public": False,
        # GitHub's manifest schema requires a hook URL even when the webhook is inactive.
        "hook_attributes": {"url": "https://example.invalid/hook", "active": False},
        "redirect_url": "https://example.invalid/",
        "default_events": [],
        "default_permissions": {"contents": "write", "metadata": "read", "pull_requests": "write"},
    }


def write_manifest(directory: Path, owner: str, repo: str, name: str) -> Path:
    payload = json.dumps(manifest(owner, repo, name))
    save(directory, "target.json", json.dumps({"owner": owner, "repo": repo}))
    save(
        directory,
        "manifest.html",
        '<!doctype html><meta charset="utf-8"><title>GitHub App setup</title>'
        '<form method="post" action="https://github.com/settings/apps/new">'
        f'<input type="hidden" name="manifest" value="{html.escape(payload, quote=True)}">'
        "<button>Create GitHub App</button></form><script>document.forms[0].submit()</script>",
    )
    print("POST form body fallback (a GET URL cannot register a manifest):")
    print(urlencode({"manifest": payload}))
    return directory / "manifest.html"


async def convert(http: httpx.AsyncClient, directory: Path, code: str) -> str:
    response = await http.post(f"{API}/app-manifests/{quote(code, safe='')}/conversions")
    response.raise_for_status()
    data = response.json()
    metadata = {"app_id": data["id"], "slug": data["slug"], "client_id": data["client_id"]}
    pem = data["pem"]
    save(directory, "private-key.pem", pem)
    save(directory, "app.json", json.dumps(metadata))
    # A newly converted app must not inherit an old installation selection.
    (directory / "installation_id").unlink(missing_ok=True)
    return f"https://github.com/apps/{metadata['slug']}/installations/new"


async def import_app(http: httpx.AsyncClient, directory: Path, app_id: int, pem: Path) -> str:
    """For an app created by hand in GitHub settings: copy its downloaded key in."""
    from sca.secrets import github_app_jwt

    key = pem.expanduser().read_text()
    response = await http.get(
        f"{API}/app",
        headers={"Authorization": f"Bearer {github_app_jwt({'app_id': app_id, 'private_key': key})}"},
    )
    response.raise_for_status()  # proves the key belongs to this app
    data = response.json()
    save(directory, "private-key.pem", key)
    save(
        directory,
        "app.json",
        json.dumps({"app_id": data["id"], "slug": data["slug"], "client_id": data["client_id"]}),
    )
    (directory / "installation_id").unlink(missing_ok=True)
    return f"https://github.com/apps/{data['slug']}/installations/new"


def credential(directory: Path) -> dict:
    data = json.loads((directory / "app.json").read_text())
    return {
        "type": "github_app",
        "app_id": data["app_id"],
        "private_key": (directory / "private-key.pem").read_text(),
        "installation_id": (directory / "installation_id").read_text().strip(),
    }


async def pages(http: httpx.AsyncClient, path: str, headers: dict):
    page = 1
    while True:
        response = await http.get(
            f"{API}{path}", params={"per_page": 100, "page": page}, headers=headers
        )
        response.raise_for_status()
        data = response.json()
        rows = data if isinstance(data, list) else data["repositories"]
        yield rows
        if len(rows) < 100:
            break
        page += 1


async def status(http: httpx.AsyncClient, directory: Path) -> None:
    from sca.config import Settings
    from sca.secrets import OpenBaoSecretProvider, github_app_jwt

    data = json.loads((directory / "app.json").read_text())
    data["private_key"] = (directory / "private-key.pem").read_text()
    target = json.loads((directory / "target.json").read_text())["repo"]
    matches = []
    provider = OpenBaoSecretProvider(Settings(), http=http)
    async for installations in pages(
        http, "/app/installations", {"Authorization": f"Bearer {github_app_jwt(data)}"}
    ):
        for installation in installations:
            entry = {**data, "installation_id": installation["id"]}
            token = await provider._installation(entry)
            async for repos in pages(
                http, "/installation/repositories", {"Authorization": f"Bearer {token.password}"}
            ):
                print(
                    f"Installation {installation['id']}: "
                    + ", ".join(r["full_name"] for r in repos)
                )
                if any(r["full_name"].lower() == target.lower() for r in repos):
                    matches.append(str(installation["id"]))
    (directory / "installation_id").unlink(missing_ok=True)
    if len(matches) != 1:
        raise ValueError("Install the app on the target repository, then run status again")
    save(directory, "installation_id", matches[0])


def store(directory: Path, tenant: str = "default") -> str:
    from sca.config import Settings
    from sca.secrets import OpenBaoSecretProvider

    if not re.fullmatch(r"[\w.-]+", tenant) or tenant in {".", ".."}:
        raise ValueError("Invalid tenant")
    settings = Settings()
    if not settings.openbao_token:
        settings.openbao_token = os.getenv("OPENBAO_DEV_ROOT_TOKEN", "sca-dev-root")
    provider = OpenBaoSecretProvider(settings)
    repo = json.loads((directory / "target.json").read_text())["repo"]
    path = f"{provider.prefix}/{tenant}/repos/github.com/{repo}"
    provider.client.secrets.kv.v2.create_or_update_secret(
        path=path, secret=credential(directory), mount_point=provider.mount
    )
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("manifest")
    create.add_argument("--owner", required=True)
    create.add_argument("--repo", required=True)
    create.add_argument("--name", default=f"sca-dev-{secrets.token_hex(4)}")
    exchange = commands.add_parser("convert")
    exchange.add_argument("--code", required=True)
    imported = commands.add_parser("import")
    imported.add_argument("--app-id", type=int, required=True)
    imported.add_argument("--pem", type=Path, required=True)
    imported.add_argument("--repo", required=True)
    commands.add_parser("status")
    seed = commands.add_parser("store")
    seed.add_argument("--tenant", default="default")
    args = parser.parse_args()
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as http:
            if args.command == "manifest":
                print(write_manifest(DATA, args.owner, args.repo, args.name))
                print(
                    f"Open the HTML while logged in as {args.owner}. Copy code from the redirect address bar; run convert within one hour, then install on the target repo, status, store."
                )
            elif args.command == "convert":
                print(await convert(http, DATA, args.code))
            elif args.command == "import":
                owner = args.repo.split("/")[0]
                manifest(owner, args.repo, "check")  # validates owner/repo
                save(DATA, "target.json", json.dumps({"owner": owner, "repo": args.repo}))
                print(await import_app(http, DATA, args.app_id, args.pem))
            elif args.command == "status":
                await status(http, DATA)
            else:
                print(f"Stored credential at {store(DATA, args.tenant)}")
    except Exception:  # noqa: BLE001 -- suppress secret-bearing API errors
        # HTTP errors can contain the one-time code or secret response body.
        parser.exit(
            1,
            "Setup failed; check local files, connectivity and app installation. No secret details printed.\n",
        )


if __name__ == "__main__":
    asyncio.run(main())
