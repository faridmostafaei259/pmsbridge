"""Entry point: runs the application with exactly one Uvicorn worker.

The app relies on in-process locks and rate limiters, so it must never run with several workers.
This module does not accept a worker count.

Forwarded headers (`X-Forwarded-For`, `X-Forwarded-Proto`) are trusted only from the addresses
listed in the `TRUSTED_PROXIES` environment variable: a comma-separated list of IP addresses or
CIDR networks, or `*` for everyone. The default is `127.0.0.1`, which trusts no other host.

The Docker Compose stack puts the app and Caddy on a private network with a fixed subnet and gives
Caddy a fixed address on it, then sets `TRUSTED_PROXIES` to that single address (Caddy's). The rest
of the subnet, including the host's gateway address, is not trusted. Do not use `*` outside
of tests.
"""

import logging
import os
import sys
from collections.abc import Mapping

import uvicorn
from fastapi import FastAPI

from pmsbridge.app import create_app

HOST = "0.0.0.0"  # noqa: S104  (inside the container; the host publishes no port for the app)
PORT = 8000
DEFAULT_TRUSTED_PROXIES = "127.0.0.1"
# Time given to in-flight requests after SIGTERM before the process exits.
GRACEFUL_SHUTDOWN_SECONDS = 20


def trusted_proxies(env: Mapping[str, str]) -> str:
    """Return the value for Uvicorn's `forwarded_allow_ips` from `TRUSTED_PROXIES`."""
    entries = [part.strip() for part in env.get("TRUSTED_PROXIES", "").split(",")]
    return ",".join(entry for entry in entries if entry) or DEFAULT_TRUSTED_PROXIES


def build_config(app: FastAPI, env: Mapping[str, str]) -> uvicorn.Config:
    """Build the Uvicorn configuration. Always one worker."""
    return uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxies(env),
        server_header=False,
        # Request URLs can carry one-time OAuth values, so Uvicorn's access log stays off.
        access_log=False,
        # Uvicorn's loggers propagate to the root logger configured in main().
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = uvicorn.Server(build_config(create_app(), os.environ))
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
