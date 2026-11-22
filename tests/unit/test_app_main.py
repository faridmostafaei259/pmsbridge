import pytest

import pmsbridge.__main__ as entry
from pmsbridge.__main__ import (
    DEFAULT_TRUSTED_PROXIES,
    HOST,
    PORT,
    build_config,
    trusted_proxies,
)
from pmsbridge.app import create_app


def test_trusted_proxies_defaults_to_loopback_only() -> None:
    assert trusted_proxies({}) == DEFAULT_TRUSTED_PROXIES == "127.0.0.1"
    assert trusted_proxies({"TRUSTED_PROXIES": ""}) == "127.0.0.1"
    assert trusted_proxies({"TRUSTED_PROXIES": " , "}) == "127.0.0.1"


def test_trusted_proxies_parses_a_list() -> None:
    value = trusted_proxies({"TRUSTED_PROXIES": " 10.0.0.0/8 , 192.168.1.5,,"})
    assert value == "10.0.0.0/8,192.168.1.5"


def test_trusted_proxies_accepts_wildcard() -> None:
    assert trusted_proxies({"TRUSTED_PROXIES": "*"}) == "*"


def test_config_runs_exactly_one_worker_on_port_8000() -> None:
    config = build_config(create_app(()), {})
    assert config.workers == 1
    assert (HOST, PORT) == ("0.0.0.0", 8000)  # noqa: S104
    assert (config.host, config.port) == ("0.0.0.0", 8000)  # noqa: S104
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "127.0.0.1"
    assert config.access_log is False
    assert config.server_header is False
    assert config.timeout_graceful_shutdown is not None


def test_config_ignores_any_worker_count_in_the_environment() -> None:
    config = build_config(create_app(()), {"WEB_CONCURRENCY": "4", "UVICORN_WORKERS": "4"})
    assert config.workers == 1


def test_config_uses_trusted_proxies_from_the_environment() -> None:
    config = build_config(create_app(()), {"TRUSTED_PROXIES": "172.18.0.0/16"})
    assert config.forwarded_allow_ips == "172.18.0.0/16"


def test_main_returns_1_with_a_clean_message_for_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://insecure-value.example")
    monkeypatch.setenv("DOMAIN", "insecure-value.example")
    started: list[bool] = []
    monkeypatch.setattr(entry.uvicorn.Server, "run", lambda self: started.append(True))

    assert entry.main() == 1
    err = capsys.readouterr().err
    assert "PUBLIC_BASE_URL" in err
    assert "insecure-value" not in err
    assert "Traceback" not in err
    assert started == []


def test_main_returns_1_without_raw_traceback_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_app() -> object:
        raise RuntimeError("feature failed to register")

    monkeypatch.setattr(entry, "create_app", failing_app)
    started: list[bool] = []
    monkeypatch.setattr(entry.uvicorn.Server, "run", lambda self: started.append(True))

    assert entry.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "startup stopped" in captured.out
    assert started == []
