"""Application settings, read from environment variables only.

There is no `.env` parsing in the application. The Docker Compose stack passes the operator's `.env`
file to the container as ordinary environment variables, and that is the only source of settings.

Every field is read from the environment variable with the same name in capitals (`data_dir` is read
from `DATA_DIR`). The configuration reference in `docs/configuration.md` is generated from the
descriptions below by `scripts/gen_config_docs.py`.

Secret values (for example the encryption keys) must use `pydantic.SecretStr`
so that printing a `Settings` object or logging it never shows them.
"""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["production", "development"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEVELOPMENT_PUBLIC_BASE_URL = "http://localhost:8000"
DEVELOPMENT_DOMAIN = "localhost"
DATABASE_FILE_NAME = "pmsbridge.db"
BACKUP_DIR_NAME = "backups"


class SettingsError(RuntimeError):
    """The configuration is invalid. The message names each variable and what is wrong with it."""


def _normalise_text(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _normalise_level(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _default_database_path(data: dict[str, object]) -> Path:
    return cast("Path", data["data_dir"]) / DATABASE_FILE_NAME


def _default_backup_dir(data: dict[str, object]) -> Path:
    return cast("Path", data["data_dir"]) / BACKUP_DIR_NAME


class Settings(BaseSettings):
    """All settings of the application. Construct it with `get_settings()`, not directly."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    environment: Annotated[Environment, BeforeValidator(_normalise_text)] = Field(
        default="production",
        description=(
            "`production` requires HTTPS and a public address that matches `DOMAIN`. "
            "`development` relaxes those checks for running on your own machine."
        ),
    )
    public_base_url: str = Field(
        default="",
        description=(
            "The full public address of this installation, for example `https://mcp.example.com`. "
            "In production it must start with `https://` and its host must equal `DOMAIN`."
        ),
        json_schema_extra={
            "required_in_production": True,
            "default_text": "none (development: `http://localhost:8000`)",
        },
    )
    domain: str = Field(
        default="",
        description=(
            "The host name people use to reach this installation, for example `mcp.example.com`."
        ),
        json_schema_extra={
            "required_in_production": True,
            "default_text": "none (development: `localhost`)",
        },
    )
    data_dir: Path = Field(
        default=Path("/data"),
        description=(
            "The folder for everything the application stores. In the container it is a volume."
        ),
    )
    database_path: Path = Field(
        default_factory=_default_database_path,
        description="The SQLite database file.",
        json_schema_extra={"default_text": "`<DATA_DIR>/pmsbridge.db`"},
    )
    backup_dir: Path = Field(
        default_factory=_default_backup_dir,
        description="The folder for automatic backups taken before a database upgrade.",
        json_schema_extra={"default_text": "`<DATA_DIR>/backups`"},
    )
    pre_migration_backups_keep: int = Field(
        default=5,
        ge=1,
        le=100,
        description="How many automatic pre-upgrade backups to keep. Older ones are deleted.",
    )
    log_level: Annotated[LogLevel, BeforeValidator(_normalise_level)] = Field(
        default="INFO",
        description=(
            "How much detail goes to the logs. "
            "Logs never contain passwords, tokens or guest details."
        ),
    )

    @field_validator("public_base_url", "domain", mode="after")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _check_public_address(self) -> "Settings":
        if self.environment == "development":
            self.public_base_url = self.public_base_url or DEVELOPMENT_PUBLIC_BASE_URL
            self.domain = self.domain or DEVELOPMENT_DOMAIN

        problems: list[str] = []
        if not self.public_base_url:
            problems.append("PUBLIC_BASE_URL is required when ENVIRONMENT is production")
        if not self.domain:
            problems.append("DOMAIN is required when ENVIRONMENT is production")
        if problems:
            raise ValueError("; ".join(problems))

        parts = urlsplit(self.public_base_url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError(
                "PUBLIC_BASE_URL must be a full web address such as https://mcp.example.com"
            )
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError(
                "PUBLIC_BASE_URL must contain only the scheme, host and optional port "
                "(no path, query or login details)"
            )
        self.public_base_url = f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}".lower()

        if self.environment == "production":
            if parts.scheme != "https":
                raise ValueError(
                    "PUBLIC_BASE_URL must start with https:// when ENVIRONMENT is production"
                )
            if host != self.domain.lower():
                raise ValueError(
                    "the host of PUBLIC_BASE_URL must equal DOMAIN when ENVIRONMENT is production"
                )
        return self


def _describe_validation_error(error: ValidationError) -> str:
    """Turn a validation error into one line per problem, naming the environment variable.

    The rejected value is deliberately left out: later settings may be secrets.
    """
    lines: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = [str(part) for part in item["loc"]]
        message = item["msg"].removeprefix("Value error, ")
        if location:
            lines.append(f"{location[0].upper()}: {message}")
        else:
            lines.append(message)
    return "\n".join(lines)


_cached: Settings | None = None
_override: Settings | None = None


def get_settings() -> Settings:
    """Return the settings, read from the environment once and then cached."""
    global _cached  # noqa: PLW0603
    if _override is not None:
        return _override
    if _cached is None:
        try:
            _cached = Settings()
        except ValidationError as exc:
            raise SettingsError(
                "Invalid configuration:\n" + _describe_validation_error(exc)
            ) from None
    return _cached


def set_settings_override(settings: Settings | None) -> None:
    """Make `get_settings()` return `settings` (tests). Pass `None` to remove the override."""
    global _override  # noqa: PLW0603
    _override = settings


def clear_settings_cache() -> None:
    """Forget the cached settings so the next `get_settings()` reads the environment again."""
    global _cached  # noqa: PLW0603
    _cached = None


@contextmanager
def override_settings(settings: Settings) -> Generator[Settings]:
    """Use `settings` inside the `with` block, then restore what was in place before."""
    global _override  # noqa: PLW0603
    previous = _override
    _override = settings
    try:
        yield settings
    finally:
        _override = previous
