"""Security invariants of the deployment files, checked from the files (no Docker needed).

These complement `tests/deploy`, which asks Docker to render the compose file. Here the YAML is
read directly, so the checks also run on machines without Docker.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

import pmsbridge.__main__ as app_main
from pmsbridge.core.crypto import generate_key

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"
CADDY_DIR = REPO_ROOT / "deploy" / "caddy"

# An `ENCRYPTION_KEYS` entry: an id, a colon and 32 bytes in base64 (43 characters and a pad).
KEY_ENTRY = re.compile(r"(?<![\w+/])[a-z0-9]{1,16}:[A-Za-z0-9+/]{43}=")


def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())


def services() -> dict[str, dict[str, Any]]:
    return compose()["services"]


@pytest.mark.parametrize("name", ["app", "caddy"])
def test_each_container_is_locked_down(name: str) -> None:
    service = services()[name]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    assert "no-new-privileges:true" in service["security_opt"]
    for forbidden in ("privileged", "pid", "ipc", "userns_mode", "devices", "cgroup_parent"):
        assert forbidden not in service
    assert service.get("network_mode") is None
    assert not any(
        "seccomp=unconfined" in option or "apparmor=unconfined" in option
        for option in service["security_opt"]
    )


@pytest.mark.parametrize("name", ["app", "caddy"])
def test_no_container_mounts_the_docker_socket_or_a_host_system_path(name: str) -> None:
    for volume in services()[name].get("volumes", []):
        source = str(volume).split(":", maxsplit=1)[0]
        assert "docker.sock" not in str(volume)
        assert source not in {"/", "/etc", "/var", "/var/run", "/proc", "/sys", "/root", "/home"}


def test_writable_tmpfs_is_small_and_the_only_writable_places_are_declared() -> None:
    for name in ("app", "caddy"):
        for entry in services()[name]["tmpfs"]:
            assert re.search(r"size=\d+m", entry), f"{name}: an unbounded tmpfs ({entry})"
    app_volumes = [str(v) for v in services()["app"]["volumes"]]
    assert app_volumes == ["app_data:/data"]


def test_only_caddy_publishes_ports_and_only_80_and_443() -> None:
    assert "ports" not in services()["app"]
    assert "expose" not in services()["app"]
    published = services()["caddy"]["ports"]
    assert len(published) == 2
    targets = sorted(str(entry).rsplit(":", maxsplit=1)[1] for entry in published)
    assert targets == ["443", "80"]
    for entry in published:
        # No host address means all interfaces, which is intended for the public edge.
        assert str(entry).startswith("${")


def test_the_admin_api_and_the_app_port_are_never_published() -> None:
    published = " ".join(str(entry) for entry in services()["caddy"]["ports"])
    assert "2019" not in published
    assert "8000" not in published
    assert "udp" not in published.lower()


def test_the_key_is_given_to_the_app_only() -> None:
    caddy = services()["caddy"]
    assert "ENCRYPTION_KEYS" not in yaml.safe_dump(caddy)
    assert "env_file" not in caddy
    assert "env_file" in services()["app"]


def test_no_command_or_entrypoint_carries_a_secret_on_the_command_line() -> None:
    for name, service in services().items():
        for field in ("command", "entrypoint"):
            joined = " ".join(str(part) for part in service.get(field, []) or [])
            assert not KEY_ENTRY.search(joined), f"{name}.{field}"
            assert "ENCRYPTION_KEYS" not in joined
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    for line in dockerfile.splitlines():
        if line.startswith(("ENTRYPOINT", "CMD")):
            assert "KEY" not in line.upper()


def test_the_app_trusts_the_single_fixed_caddy_address_by_default() -> None:
    value = services()["app"]["environment"]["TRUSTED_PROXIES"]
    assert "*" not in value
    assert "/" not in value, "a subnet would also trust the host gateway address"
    assert "CADDY_BACKEND_IP" in value


def test_both_containers_share_one_private_bridge_network_and_nothing_else() -> None:
    network = compose()["networks"]["backend"]
    assert not network.get("external")
    assert "driver_opts" not in network
    for name in ("app", "caddy"):
        attached = services()[name]["networks"]
        assert list(attached) == ["backend"]


def test_the_dockerfile_never_bakes_in_secrets_or_runs_as_root() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text()
    # Join backslash continuations so a multi-line ENV or ARG is checked as one instruction.
    joined = re.sub(r"\\\n\s*", " ", text)
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ENV", "ARG")):
            assert not re.search(r"(?i)(secret|password|token|key)\w*\s*=", stripped), stripped
    user_lines = [line for line in joined.splitlines() if line.startswith("USER ")]
    assert user_lines
    assert user_lines[-1].split()[1].split(":")[0] not in {"root", "0"}
    assert "--privileged" not in joined


def test_caddy_serves_no_directory_listing_and_hides_readiness_and_server_name() -> None:
    site = (CADDY_DIR / "site.caddy").read_text()
    assert "file_server" not in site
    assert "browse" not in site
    assert "/readyz" in site
    assert "header -Server" in site
    assert re.search(r"max_size\s+\d+MB", site)


@pytest.mark.parametrize("name", ["Caddyfile.auto", "Caddyfile.internal"])
def test_caddy_global_options_keep_access_logs_off_and_http3_off(name: str) -> None:
    text = (CADDY_DIR / name).read_text()
    assert "protocols h1 h2" in text
    # Request URLs can carry one-time OAuth values, so no access log block is configured.
    assert "log access" not in text
    assert not re.search(r"^\s*log\s*\{[^}]*level\s+DEBUG", text, re.MULTILINE | re.IGNORECASE)
    assert "debug" not in text.lower().replace("# ", "")


def test_internal_mode_sends_no_hsts_and_auto_mode_does_not_include_subdomains() -> None:
    assert "Strict-Transport-Security" not in (CADDY_DIR / "Caddyfile.internal").read_text()
    auto = (CADDY_DIR / "Caddyfile.auto").read_text()
    assert "Strict-Transport-Security" in auto
    assert "includeSubDomains" not in auto
    assert "preload" not in auto


def test_no_tracked_file_contains_a_key_shaped_value() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607  (git from PATH)
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    offenders: list[str] = []
    for name in result.stdout.decode().split("\0"):
        path = REPO_ROOT / name
        if not name or not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if path.name == "uv.lock" or "tests/security/" in name:
            continue
        if KEY_ENTRY.search(text):
            offenders.append(name)
    assert not offenders, f"key-shaped values in: {offenders}"


def _start(monkeypatch: pytest.MonkeyPatch, trusted: str) -> tuple[int, list[bool]]:
    """Run the real entry point with valid production settings and a stub server."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DOMAIN", "mcp.example.test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("sec1"))
    monkeypatch.setenv("TRUSTED_PROXIES", trusted)
    started: list[bool] = []
    monkeypatch.setattr(app_main, "create_app", FastAPI)
    monkeypatch.setattr(app_main.uvicorn.Server, "run", lambda self: started.append(True))
    return app_main.main(), started


@pytest.mark.usefixtures("restore_logging")
def test_a_single_trusted_proxy_address_starts_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, started = _start(monkeypatch, "172.31.250.10")
    assert (code, started) == (0, [True])


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0"])
def test_startup_refuses_a_wildcard_trusted_proxy_in_production(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    code, started = _start(monkeypatch, value)
    assert code == 1
    assert started == []
    assert "TRUSTED_PROXIES" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["Caddyfile.auto", "Caddyfile.internal"])
def test_caddy_error_log_filters_query_strings_and_sensitive_headers(name: str) -> None:
    """Caddy's error log comes from the default logger, which the global `log` block configures.

    The block must import the shared filter, and the filter must cut the query string from the URI
    and remove the request headers, which is how X-CSRF-Token, Proxy-Authorization and any header
    named like a token, secret, signature or session are kept out of the log.
    """
    text = (CADDY_DIR / name).read_text()
    block = re.search(r"^\tlog \{(.*?)^\t\}", text, re.MULTILINE | re.DOTALL)
    assert block is not None, "no global log block"
    assert "import /etc/caddy/log-filter.caddy" in block.group(1)

    shared = (CADDY_DIR / "log-filter.caddy").read_text()
    assert re.search(r"^format filter \{", shared, re.MULTILINE)
    assert re.search(r"^\twrap json$", shared, re.MULTILINE)
    assert re.search(r'^\t\trequest>uri regexp "\\\?\.\*" ""$', shared, re.MULTILINE)
    assert re.search(r"^\t\trequest>headers delete$", shared, re.MULTILINE)
    assert "exclude admin.api" in shared


@pytest.mark.parametrize("name", ["site.caddy", "Caddyfile.auto", "Caddyfile.internal"])
def test_no_caddy_log_directive_exists_outside_the_global_options_block(name: str) -> None:
    """A site-level `log` would be an access log that bypasses the shared filter."""
    depth = 0
    global_block = False  # inside the first top-level bare `{` block
    global_done = False
    for number, raw in enumerate((CADDY_DIR / name).read_text().splitlines(), start=1):
        line = raw.split("#", maxsplit=1)[0].strip()
        if re.match(r"log\b", line):
            assert name != "site.caddy", f"{name}:{number}: log directive in the shared site body"
            assert global_block and depth == 1, f"{name}:{number}: log outside the global block"
        if depth == 0 and line == "{" and not global_done:
            global_block = True
        depth += line.count("{") - line.count("}")
        if global_block and depth == 0:
            global_block, global_done = False, True
