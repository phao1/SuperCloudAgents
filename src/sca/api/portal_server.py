"""Run the Portal API with a known Uvicorn listener configuration."""

import argparse
from functools import partial

import uvicorn

from sca.api.app import create_app
from sca.api.authz import is_loopback


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--proxy-headers", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    local_portal_port = args.port if is_loopback(args.host) and not args.proxy_headers else None
    uvicorn.run(partial(create_app, local_portal_port=local_portal_port), factory=True,
                host=args.host, port=args.port, proxy_headers=args.proxy_headers)


if __name__ == "__main__":
    main()
