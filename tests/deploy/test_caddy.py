import json

import pytest
from deploy_helpers import adapted_caddy_config, requires_docker, run_caddy

pytestmark = requires_docker

AUTO_ENV = {"DOMAIN": "mcp.example.com", "ACME_EMAIL": "ops@example.com", "ACME_CA": "production"}
INTERNAL_ENV = {"DOMAIN": "mcp.example.com"}


@pytest.mark.parametrize(
    ("mode", "env"), [("auto", AUTO_ENV), ("internal", INTERNAL_ENV)], ids=["auto", "internal"]
)
def test_caddyfile_validates(caddy_available: str, mode: str, env: dict[str, str]) -> None:
    result = run_caddy(caddy_available, mode, "validate", env)
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "Valid configuration" in output
    assert "not formatted" not in output


def test_auto_mode_sends_hsts_and_internal_mode_does_not(caddy_available: str) -> None:
    auto = json.dumps(adapted_caddy_config(caddy_available, "auto", AUTO_ENV))
    internal = json.dumps(adapted_caddy_config(caddy_available, "internal", INTERNAL_ENV))
    assert "Strict-Transport-Security" in auto
    assert "Strict-Transport-Security" not in internal


@pytest.mark.parametrize(
    ("mode", "env"), [("auto", AUTO_ENV), ("internal", INTERNAL_ENV)], ids=["auto", "internal"]
)
def test_proxy_target_and_protections(caddy_available: str, mode: str, env: dict[str, str]) -> None:
    text = json.dumps(adapted_caddy_config(caddy_available, mode, env))
    assert '"dial": "app:8000"' in text
    assert '"max_size": 2000000' in text
    assert '"protocols": ["h1", "h2"]' in text


def test_internal_mode_uses_the_internal_issuer(caddy_available: str) -> None:
    config = adapted_caddy_config(caddy_available, "internal", INTERNAL_ENV)
    text = json.dumps(config)
    assert '"module": "internal"' in text
    assert "acme" not in text


def test_auto_mode_uses_default_authorities_unless_staging_is_requested(
    caddy_available: str,
) -> None:
    production = json.dumps(adapted_caddy_config(caddy_available, "auto", AUTO_ENV))
    assert "acme-staging" not in production
    assert "acme.zerossl.com" in production  # fallback authority
    staging_env = {**AUTO_ENV, "ACME_CA": "staging"}
    staging = json.dumps(adapted_caddy_config(caddy_available, "auto", staging_env))
    assert "https://acme-staging-v02.api.letsencrypt.org/directory" in staging
    assert "zerossl" not in staging


def test_unknown_acme_ca_value_fails_loudly(caddy_available: str) -> None:
    result = run_caddy(caddy_available, "auto", "validate", {**AUTO_ENV, "ACME_CA": "stagin"})
    assert result.returncode != 0
    assert "stagin.caddy" in result.stderr


def test_auto_mode_requires_an_email(caddy_available: str) -> None:
    result = run_caddy(caddy_available, "auto", "validate", {**AUTO_ENV, "ACME_EMAIL": ""})
    assert result.returncode != 0
    assert "email" in result.stderr


def _site_routes(config: dict[str, object]) -> list[dict[str, object]]:
    """The routes inside the single site's subroute, in the order Caddy evaluates them."""
    servers = config["apps"]["http"]["servers"]  # type: ignore[index]
    site = next(iter(servers.values()))["routes"][0]
    routes: list[dict[str, object]] = site["handle"][0]["routes"]
    return routes


@pytest.mark.parametrize(
    ("mode", "env"), [("auto", AUTO_ENV), ("internal", INTERNAL_ENV)], ids=["auto", "internal"]
)
def test_readyz_is_answered_by_caddy_and_never_proxied(
    caddy_available: str, mode: str, env: dict[str, str]
) -> None:
    routes = _site_routes(adapted_caddy_config(caddy_available, mode, env))

    def has_path(route: dict[str, object], path: str) -> bool:
        return any(path in m.get("path", []) for m in route.get("match", []))  # type: ignore[attr-defined]

    blocked = [i for i, r in enumerate(routes) if has_path(r, "/readyz")]
    proxied = [i for i, r in enumerate(routes) if "reverse_proxy" in json.dumps(r)]
    assert len(blocked) == 1
    assert len(proxied) == 1
    # The block comes first, and both are in one group so that only the first match runs.
    assert blocked[0] < proxied[0]
    assert routes[blocked[0]]["group"] == routes[proxied[0]]["group"]
    blocked_route = json.dumps(routes[blocked[0]])
    assert '"handler": "static_response"' in blocked_route
    assert '"status_code": 404' in blocked_route
    assert "reverse_proxy" not in blocked_route
    # The proxy route has no matcher, so it catches everything except the blocked path.
    assert "match" not in routes[proxied[0]]
