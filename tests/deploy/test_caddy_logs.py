"""Live check of Caddy's error log: query strings and sensitive headers must not be written.

Caddy runs alone, without a network, so the upstream `app` cannot be resolved and every proxied
request ends as a 502 in Caddy's error log. The request is sent from inside the container, so no
port is published. `localhost` makes Caddy use its own authority in both modes (no certificate
authority is contacted).
"""

import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from deploy_helpers import CADDY_DIR, requires_docker, run_docker

pytestmark = requires_docker

ENV = {"DOMAIN": "localhost", "ACME_EMAIL": "ops@example.com", "ACME_CA": "production"}
SENTINELS = ("SENTINEL-QUERY-TOKEN", "SENTINEL-QUERY-CODE", "SENTINEL-CSRF", "SENTINEL-SESSION")
STARTUP_SECONDS = 30

# Starts Caddy detached for a config directory and a mode, and returns the container name.
StartCaddy = Callable[[Path, str], str]


@pytest.fixture
def run_caddy_detached(caddy_available: str) -> Iterator[StartCaddy]:
    """Start Caddy in a throwaway container, and remove the container afterwards."""
    started: list[str] = []

    def start(config_dir: Path, mode: str) -> str:
        name = f"pmsbridge-test-caddy-{uuid.uuid4().hex[:10]}"
        args = ["run", "-d", "--rm", "--name", name, "--network", "none"]
        args += ["-v", f"{config_dir}:/etc/caddy:ro"]
        for key, value in ENV.items():
            args += ["-e", f"{key}={value}"]
        args += [caddy_available, "caddy", "run", "--config", f"/etc/caddy/Caddyfile.{mode}"]
        args += ["--adapter", "caddyfile"]
        result = run_docker(*args)
        assert result.returncode == 0, result.stderr
        started.append(name)
        return name

    yield start
    for name in started:
        run_docker("stop", "--time", "2", name)


def _wait_until_serving(name: str) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        probe = run_docker(
            "exec", name, "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:2019/config/"
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    pytest.fail("Caddy did not start: " + run_docker("logs", name).stderr[-500:])


def _error_log_after_a_request_with_sentinels(name: str) -> str:
    _wait_until_serving(name)
    query = f"token={SENTINELS[0]}&code={SENTINELS[1]}"
    # Retry briefly: the listener may need a moment after the admin API answers.
    for _ in range(20):
        request = run_docker(
            "exec",
            name,
            "wget",
            "-q",
            "-O",
            "/dev/null",
            "--no-check-certificate",
            "--header",
            f"X-CSRF-Token: {SENTINELS[2]}",
            "--header",
            f"X-Session-Id: {SENTINELS[3]}",
            f"https://localhost/x?{query}",
        )
        if "502" in request.stderr:
            break
        time.sleep(0.5)
    else:
        pytest.fail("no 502 answer from Caddy: " + request.stderr)
    logs = run_docker("logs", name)
    return logs.stdout + logs.stderr


@pytest.mark.parametrize("mode", ["auto", "internal"])
def test_error_log_has_the_path_but_no_query_string_or_sensitive_header(
    run_caddy_detached: StartCaddy, mode: str
) -> None:
    name: str = run_caddy_detached(CADDY_DIR, mode)
    logs = _error_log_after_a_request_with_sentinels(name)
    error_lines = [line for line in logs.splitlines() if '"logger":"http.log.error"' in line]
    assert error_lines, "no error log line was written"
    assert '"uri":"/x"' in error_lines[0]
    for sentinel in SENTINELS:
        assert sentinel not in logs
    assert '"headers"' not in error_lines[0]


def test_the_check_would_notice_a_leak(tmp_path: Path, run_caddy_detached: StartCaddy) -> None:
    """Control: with the filter replaced by a plain JSON log, the sentinels do show up."""
    for source in CADDY_DIR.iterdir():
        if source.is_dir():
            shutil.copytree(source, tmp_path / source.name)
        else:
            shutil.copy(source, tmp_path / source.name)
    (tmp_path / "log-filter.caddy").write_text("output stdout\nformat json\nexclude admin.api\n")
    name: str = run_caddy_detached(tmp_path, "internal")
    logs = _error_log_after_a_request_with_sentinels(name)
    assert re.search(r"SENTINEL-QUERY-TOKEN", logs)
    assert SENTINELS[2] in logs
