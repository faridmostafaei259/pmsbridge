import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from pmsbridge.core.settings import (
    Settings,
    SettingsError,
    get_settings,
    override_settings,
    set_settings_override,
)

ROOT = Path(__file__).resolve().parents[3]


def production(**overrides: str) -> Settings:
    values = {"public_base_url": "https://mcp.example.com", "domain": "mcp.example.com"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_production_is_the_default_and_defaults_follow_data_dir() -> None:
    settings = production(data_dir="/srv/pms")  # type: ignore[arg-type]
    assert settings.environment == "production"
    assert settings.data_dir == Path("/srv/pms")
    assert settings.database_path == Path("/srv/pms/pmsbridge.db")
    assert settings.backup_dir == Path("/srv/pms/backups")
    assert settings.pre_migration_backups_keep == 5
    assert settings.log_level == "INFO"


def test_default_data_dir_is_slash_data() -> None:
    assert production().data_dir == Path("/data")


def test_explicit_paths_win_over_data_dir() -> None:
    settings = production(database_path="/x/app.db", backup_dir="/y/bk")  # type: ignore[arg-type]
    assert settings.database_path == Path("/x/app.db")
    assert settings.backup_dir == Path("/y/bk")


def test_production_accepts_https_with_matching_domain_and_port() -> None:
    settings = production(public_base_url="https://MCP.example.com:8443/")
    assert settings.public_base_url == "https://mcp.example.com:8443"


def test_production_rejects_http() -> None:
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL must start with https://"):
        production(public_base_url="http://mcp.example.com")


def test_production_rejects_a_host_that_differs_from_domain() -> None:
    with pytest.raises(ValueError, match="host of PUBLIC_BASE_URL must equal DOMAIN") as caught:
        production(public_base_url="https://other.example.com")
    assert "other.example.com" not in str(caught.value).split("[type=")[0]


@pytest.mark.parametrize("value", ["mcp.example.com", "ftp://mcp.example.com", "https://"])
def test_public_base_url_must_be_a_web_address(value: str) -> None:
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL must be a full web address"):
        production(public_base_url=value)


@pytest.mark.parametrize(
    "value",
    ["https://mcp.example.com/app", "https://mcp.example.com/?x=1", "https://u:p@mcp.example.com"],
)
def test_public_base_url_has_no_path_query_or_login(value: str) -> None:
    with pytest.raises(ValueError, match="only the scheme, host and optional port"):
        production(public_base_url=value)


def test_production_requires_both_variables() -> None:
    with pytest.raises(ValueError) as caught:
        Settings()
    assert "PUBLIC_BASE_URL is required" in str(caught.value)
    assert "DOMAIN is required" in str(caught.value)


def test_development_is_relaxed() -> None:
    settings = Settings(environment="development")
    assert settings.public_base_url == "http://localhost:8000"
    assert settings.domain == "localhost"
    relaxed = Settings(
        environment="development", public_base_url="http://example.test:9000", domain="other"
    )
    assert relaxed.public_base_url == "http://example.test:9000"


def test_values_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "Development")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("DATA_DIR", "/somewhere")
    monkeypatch.setenv("PRE_MIGRATION_BACKUPS_KEEP", "2")
    settings = Settings()
    assert settings.environment == "development"
    assert settings.log_level == "DEBUG"
    assert settings.database_path == Path("/somewhere/pmsbridge.db")
    assert settings.pre_migration_backups_keep == 2


def test_unrelated_environment_variables_and_env_files_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("ENVIRONMENT=development\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TLS_MODE", "internal")
    with pytest.raises(ValueError, match="required"):
        Settings()


def test_get_settings_names_the_variable_and_hides_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://secret-looking.example.com")
    monkeypatch.setenv("DOMAIN", "mcp.example.com")
    monkeypatch.setenv("PRE_MIGRATION_BACKUPS_KEEP", "zero")
    with pytest.raises(SettingsError) as caught:
        get_settings()
    message = str(caught.value)
    assert "PRE_MIGRATION_BACKUPS_KEEP" in message
    assert "zero" not in message
    assert caught.value.__cause__ is None


def test_get_settings_is_cached_and_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    first = get_settings()
    assert get_settings() is first
    replacement = Settings(environment="development", log_level="ERROR")
    with override_settings(replacement):
        assert get_settings() is replacement
    assert get_settings() is first
    set_settings_override(replacement)
    assert get_settings() is replacement
    set_settings_override(None)
    assert get_settings() is first


def test_invalid_environment_value_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(SettingsError, match="ENVIRONMENT"):
        get_settings()


# ---- Generated configuration reference ----


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", ROOT / "scripts" / "gen_config_docs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_generator()
    output = tmp_path / "configuration.md"
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def test_config_docs_check_passes_after_generation(generator: ModuleType) -> None:
    assert generator.main(["--check"]) == 1
    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0


def test_config_docs_check_fails_when_stale(generator: ModuleType) -> None:
    generator.main([])
    generator.OUTPUT.write_text(generator.OUTPUT.read_text() + "hand edit\n")
    assert generator.main(["--check"]) == 1


def test_committed_config_docs_are_current() -> None:
    assert load_generator().main(["--check"]) == 0


def test_config_docs_cover_every_setting_and_compose_variable() -> None:
    text = load_generator().render()
    for name in Settings.model_fields:
        assert f"`{name.upper()}`" in text
    compose = (ROOT / "deploy" / "compose.yaml").read_text()
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
    missing = {name for name in used if f"`{name}`" not in text}
    assert not missing, f"undocumented variables in deploy/compose.yaml: {sorted(missing)}"
    assert "Generated by scripts/gen_config_docs.py" in text
