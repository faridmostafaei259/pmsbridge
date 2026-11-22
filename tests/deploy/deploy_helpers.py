"""Helpers for the deployment tests.

Tests that need Docker skip cleanly when it is not available. None of them publish a port.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY_DIR / "compose.yaml"
CADDY_DIR = DEPLOY_DIR / "caddy"

DOCKER_TIMEOUT_SECONDS = 120


def run_docker(
    *args: str, timeout: int = DOCKER_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [docker, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            run_docker("info", timeout=30).returncode == 0
            and run_docker("compose", "version", timeout=30).returncode == 0
        )
    except subprocess.TimeoutExpired:
        return False


DOCKER_AVAILABLE = _docker_compose_available()
requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="Docker with Compose v2 is not available"
)


def caddy_image() -> str:
    """The pinned Caddy base image of deploy/caddy/Dockerfile (single source of truth)."""
    text = (CADDY_DIR / "Dockerfile").read_text()
    match = re.search(r"^FROM\s+(caddy:\S+)\s*$", text, re.MULTILINE)
    assert match is not None, "deploy/caddy/Dockerfile must pin a caddy base image"
    return match.group(1)


def run_caddy(
    image: str, mode: str, command: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run `caddy validate` or `caddy adapt` for Caddyfile.<mode> inside the pinned image."""
    args = ["run", "--rm", "-v", f"{CADDY_DIR}:/etc/caddy:ro"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += [image, "caddy", command, "--config", f"/etc/caddy/Caddyfile.{mode}"]
    args += ["--adapter", "caddyfile"]
    return run_docker(*args)


def adapted_caddy_config(image: str, mode: str, env: dict[str, str]) -> dict[str, object]:
    result = run_caddy(image, mode, "adapt", env)
    assert result.returncode == 0, result.stderr
    config: dict[str, object] = json.loads(result.stdout)
    return config


def compose_config(env_file: Path) -> subprocess.CompletedProcess[str]:
    return run_docker(
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE_FILE),
        "config",
        "--format",
        "json",
        timeout=60,
    )
