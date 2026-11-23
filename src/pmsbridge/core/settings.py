"""Application settings, read from environment variables only.

There is no `.env` parsing in the application. The Docker Compose stack passes the operator's `.env`
file to the container as ordinary environment variables, and that is the only source of settings.

Every field is read from the environment variable with the same name in capitals (`data_dir` is read
from `DATA_DIR`). The configuration reference in `docs/configuration.md` is generated from the
descriptions below by `scripts/gen_config_docs.py`.

Secret values (for example `ENCRYPTION_KEYS`) must use `pydantic.SecretStr` so that printing a
`Settings` object or logging it never shows them.
"""

import base64
import binascii
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

from pydantic import (
    BeforeValidator,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["production", "development"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEVELOPMENT_PUBLIC_BASE_URL = "http://localhost:8000"
DEVELOPMENT_DOMAIN = "localhost"
DATABASE_FILE_NAME = "pmsbridge.db"
BACKUP_DIR_NAME = "backups"
ENCRYPTION_KEY_BYTES = 32
KEY_ID_PATTERN = re.compile(r"[a-z0-9]{1,16}")


class SettingsError(RuntimeError):
    """The configuration is invalid. The message names each variable and what is wrong with it."""


def _normalise_text(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _normalise_level(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def parse_encryption_keys(value: str) -> list[tuple[str, bytes]]:
    """Parse `ENCRYPTION_KEYS` into `(key_id, key)` pairs, the active key first.

    The error messages say which entry is wrong but never repeat any part of the value.
    """
    if not value.strip():
        return []
    keys: list[tuple[str, bytes]] = []
    for number, entry in enumerate(value.split(","), start=1):
        key_id, separator, encoded = entry.strip().partition(":")
        if not separator or not KEY_ID_PATTERN.fullmatch(key_id):
            raise ValueError(
                f"entry {number} must look like <id>:<key>, where the id is 1 to 16 characters "
                "from a-z and 0-9"
            )
        try:
            key = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise ValueError(f"entry {number} does not contain a valid base64 key") from None
        if len(key) != ENCRYPTION_KEY_BYTES:
            raise ValueError(f"entry {number} is not a {ENCRYPTION_KEY_BYTES * 8}-bit key")
        if any(key_id == existing for existing, _ in keys):
            raise ValueError(f"entry {number} repeats the id of an earlier entry")
        keys.append((key_id, key))
    return keys


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
    audit_retention_days: int = Field(
        default=90,
        ge=7,
        le=3650,
        description="Keep the record of what AI assistants did for this many days, then delete it.",
    )
    guesty_token_refresh_minutes: int = Field(
        default=45,
        ge=30,
        le=60,
        description=(
            "Refresh a Guesty access token this many minutes before it expires. Guesty allows "
            "only five new tokens per day for each API key, so tokens are reused until then."
        ),
    )
    pms_max_concurrency: int = Field(
        default=8,
        ge=1,
        le=15,
        description="How many requests may run at the same time against one PMS account.",
    )
    session_idle_hours: int = Field(
        default=12,
        ge=1,
        le=168,
        description="Sign staff out after this many hours without activity in the dashboard.",
    )
    session_max_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Sign staff out this many days after they signed in, even if they stay active.",
    )
    log_level: Annotated[LogLevel, BeforeValidator(_normalise_level)] = Field(
        default="INFO",
        description=(
            "How much detail goes to the logs. "
            "Logs never contain passwords, tokens or guest details."
        ),
    )

    encryption_keys: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "The keys that protect stored PMS credentials and tokens: comma-separated entries "
            "of the form `<id>:<key>`, where the key is 32 random bytes in base64. The first "
            "entry encrypts new data; later entries are old keys kept only to read older data. "
            "If you lose these keys, the stored credentials cannot be recovered, so back them "
            "up. Key rotation commands arrive with the `pmsbridge rotate-keys` host command in a "
            "later release; until then keep the single key."
        ),
        json_schema_extra={
            "required_in_production": True,
            "default_text": "none (development: optional)",
        },
    )

    @field_validator("public_base_url", "domain", mode="after")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("encryption_keys", mode="after")
    @classmethod
    def _check_encryption_keys(cls, value: SecretStr) -> SecretStr:
        parse_encryption_keys(value.get_secret_value())
        return value

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
        keys_missing = (
            self.environment == "production" and not self.encryption_keys.get_secret_value().strip()
        )
        keys_problem = "ENCRYPTION_KEYS is required when ENVIRONMENT is production"
        if problems:
            if keys_missing:
                problems.append(keys_problem)
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
        if keys_missing:
            raise ValueError(keys_problem)
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
