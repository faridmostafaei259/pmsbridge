"""Fixtures for the deployment tests."""

from pathlib import Path

import pytest
from deploy_helpers import DOCKER_AVAILABLE, caddy_image, run_docker


@pytest.fixture(scope="session")
def caddy_available() -> str:
    """Make sure the pinned Caddy image is present locally, pulling it if needed."""
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker with Compose v2 is not available")
    image = caddy_image()
    if run_docker("image", "inspect", image).returncode != 0:
        pulled = run_docker("pull", "--quiet", image, timeout=300)
        if pulled.returncode != 0:
            pytest.skip(f"cannot pull {image}: {pulled.stderr.strip()[:200]}")
    return image


@pytest.fixture
def compose_env_file(tmp_path: Path) -> Path:
    """A settings file with fake values, in a temporary directory."""
    service_env = tmp_path / "service.env"
    service_env.write_text("PUBLIC_BASE_URL=https://localhost:8443\n")
    env = tmp_path / "compose.env"
    env.write_text(
        "DOMAIN=localhost\n"
        "TLS_MODE=internal\n"
        "HTTP_PORT=8080\n"
        "HTTPS_PORT=8443\n"
        f"PMSBRIDGE_ENV_FILE={service_env}\n"
    )
    return env
