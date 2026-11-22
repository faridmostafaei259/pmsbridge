import json
from pathlib import Path

from deploy_helpers import COMPOSE_FILE, compose_config, requires_docker

pytestmark = requires_docker


def _config(env_file: Path) -> dict[str, dict[str, dict[str, object]]]:
    result = compose_config(env_file)
    assert result.returncode == 0, result.stderr
    config: dict[str, dict[str, dict[str, object]]] = json.loads(result.stdout)
    return config


def test_compose_file_is_valid(compose_env_file: Path) -> None:
    config = _config(compose_env_file)
    assert set(config["services"]) == {"caddy", "app"}


def test_only_caddy_publishes_ports(compose_env_file: Path) -> None:
    services = _config(compose_env_file)["services"]
    assert "ports" not in services["app"]
    published = {(p["target"], p["published"]) for p in services["caddy"]["ports"]}  # type: ignore[attr-defined]
    assert published == {(80, "8080"), (443, "8443")}


def test_host_ports_default_to_80_and_443(tmp_path: Path) -> None:
    service_env = tmp_path / "service.env"
    service_env.write_text("PUBLIC_BASE_URL=https://mcp.example.com\n")
    env = tmp_path / "compose.env"
    env.write_text(f"DOMAIN=mcp.example.com\nPMSBRIDGE_ENV_FILE={service_env}\n")
    services = _config(env)["services"]
    published = {(p["target"], p["published"]) for p in services["caddy"]["ports"]}  # type: ignore[attr-defined]
    assert published == {(80, "80"), (443, "443")}
    assert services["caddy"]["command"][3] == "/etc/caddy/Caddyfile.auto"  # type: ignore[index]


def test_tls_mode_selects_the_caddyfile(compose_env_file: Path) -> None:
    services = _config(compose_env_file)["services"]
    assert "/etc/caddy/Caddyfile.internal" in services["caddy"]["command"]  # type: ignore[operator]


def test_services_restart_and_have_healthchecks_and_log_rotation(compose_env_file: Path) -> None:
    for name, service in _config(compose_env_file)["services"].items():
        assert service["restart"] == "unless-stopped", name
        assert "test" in service["healthcheck"], name  # type: ignore[operator]
        assert service["logging"]["driver"] == "json-file", name  # type: ignore[index]
        options = service["logging"]["options"]  # type: ignore[index]
        assert options["max-size"] and options["max-file"], name


def test_containers_are_locked_down(compose_env_file: Path) -> None:
    services = _config(compose_env_file)["services"]
    for name in ("app", "caddy"):
        assert services[name]["read_only"] is True, name
        assert services[name]["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in services[name]["security_opt"], name  # type: ignore[operator]
        assert services[name].get("cap_add") is None, name


def test_state_lives_in_named_volumes(compose_env_file: Path) -> None:
    config = _config(compose_env_file)
    assert set(config["volumes"]) == {"app_data", "caddy_data", "caddy_config"}
    app_mounts = {v["target"]: v["source"] for v in config["services"]["app"]["volumes"]}  # type: ignore[attr-defined]
    assert app_mounts == {"/data": "app_data"}
    caddy_mounts = {v["target"] for v in config["services"]["caddy"]["volumes"]}  # type: ignore[attr-defined]
    assert {"/data", "/config", "/etc/caddy"} <= caddy_mounts


def test_app_trusts_only_the_fixed_caddy_address(compose_env_file: Path) -> None:
    config = _config(compose_env_file)
    services = config["services"]
    caddy_ip = services["caddy"]["networks"]["backend"]["ipv4_address"]  # type: ignore[index]
    assert caddy_ip == "172.31.250.10"
    trusted = services["app"]["environment"]["TRUSTED_PROXIES"]  # type: ignore[index]
    assert trusted == caddy_ip
    assert trusted != "*"
    assert "/" not in trusted  # a single address, never the whole subnet
    assert services["app"]["networks"] == {"backend": None}
    subnets = [c["subnet"] for c in config["networks"]["backend"]["ipam"]["config"]]  # type: ignore[index]
    assert subnets == ["172.31.250.0/24"]
    assert config["networks"]["backend"].get("enable_ipv6") in (None, False)


def test_trusted_address_follows_an_overridden_caddy_address(tmp_path: Path) -> None:
    service_env = tmp_path / "service.env"
    service_env.write_text("")
    env = tmp_path / "compose.env"
    env.write_text(
        "DOMAIN=localhost\n"
        "BACKEND_SUBNET=10.99.0.0/24\n"
        "CADDY_BACKEND_IP=10.99.0.10\n"
        f"PMSBRIDGE_ENV_FILE={service_env}\n"
    )
    services = _config(env)["services"]
    assert services["app"]["environment"]["TRUSTED_PROXIES"] == "10.99.0.10"  # type: ignore[index]
    assert services["caddy"]["networks"]["backend"]["ipv4_address"] == "10.99.0.10"  # type: ignore[index]


def test_trusted_proxies_can_be_set_in_the_settings_file(tmp_path: Path) -> None:
    service_env = tmp_path / "service.env"
    service_env.write_text("")
    env = tmp_path / "compose.env"
    env.write_text(
        f"DOMAIN=localhost\nTRUSTED_PROXIES=192.0.2.7\nPMSBRIDGE_ENV_FILE={service_env}\n"
    )
    app = _config(env)["services"]["app"]
    assert app["environment"]["TRUSTED_PROXIES"] == "192.0.2.7"  # type: ignore[index]


def test_caddy_runs_as_the_fixed_non_root_user_without_capabilities(
    compose_env_file: Path,
) -> None:
    caddy = _config(compose_env_file)["services"]["caddy"]
    assert caddy["user"] == "10001:10001"
    assert caddy["cap_drop"] == ["ALL"]
    assert "cap_add" not in caddy
    assert caddy["read_only"] is True
    assert "no-new-privileges:true" in caddy["security_opt"]  # type: ignore[operator]
    assert caddy["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": "0"}
    assert caddy["build"]["dockerfile"] == "Dockerfile"  # type: ignore[index]


def test_missing_domain_is_reported(tmp_path: Path) -> None:
    service_env = tmp_path / "service.env"
    service_env.write_text("")
    env = tmp_path / "compose.env"
    env.write_text(f"PMSBRIDGE_ENV_FILE={service_env}\n")
    result = compose_config(env)
    assert result.returncode != 0
    assert "DOMAIN" in result.stderr


def test_missing_settings_file_is_an_error(tmp_path: Path) -> None:
    env = tmp_path / "compose.env"
    env.write_text(f"DOMAIN=localhost\nPMSBRIDGE_ENV_FILE={tmp_path / 'absent.env'}\n")
    result = compose_config(env)
    assert result.returncode != 0


def test_compose_file_exists() -> None:
    assert COMPOSE_FILE.is_file()
