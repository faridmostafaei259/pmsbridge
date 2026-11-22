"""Attempts to push secrets through the logging pipeline end to end (JSON on standard output).

Covers the one secret that exists today, an `ENCRYPTION_KEYS` entry, in every place it could end
up: message text, format arguments, extra fields, exceptions, chained exceptions and settings
errors.
"""

import json
import logging

import pytest
from pydantic import SecretStr

from pmsbridge.core.logging import configure_logging
from pmsbridge.core.settings import (
    Settings,
    SettingsError,
    clear_settings_cache,
    get_settings,
)

pytestmark = pytest.mark.usefixtures("restore_logging")


def key_of(entry: str) -> str:
    return entry.partition(":")[2]


def output(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def test_a_key_is_never_written_by_any_logging_call_style(
    secure_settings: Settings, key_entry: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(secure_settings)
    log = logging.getLogger("security.test")
    key = key_of(key_entry)
    log.info("plain %s", key_entry)
    log.info(f"f-string {key_entry}")
    log.info("dict arg %(k)s", {"k": key_entry})
    log.info("x", extra={"detail": key_entry})
    log.info("x", extra={"nested": {"deeper": [key_entry, {"again": key_entry}]}})
    log.info("x", extra={"encryption_keys": key})
    log.info("secret object", extra={"value": SecretStr(key)})
    log.info("tuple %s %r", (key_entry,), [key_entry])
    log.info("ENCRYPTION_KEYS=%s,old1:%s", key_entry, key_entry.replace("sec1", "old2"))
    log.warning("url https://host/path?ENCRYPTION_KEYS=%s&a=1", key_entry)
    assert key not in output(capsys)


def test_a_key_in_an_exception_message_or_a_chained_exception_is_masked(
    secure_settings: Settings, key_entry: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(secure_settings)
    log = logging.getLogger("security.test")
    key = key_of(key_entry)
    try:
        try:
            raise ValueError(f"inner failure with {key_entry}")
        except ValueError as inner:
            raise RuntimeError(f"outer failure ENCRYPTION_KEYS={key_entry}") from inner
    except RuntimeError:
        log.exception("operation failed: %s", key_entry)
    log.error("with stack", stack_info=True, extra={"detail": key_entry})
    assert key not in output(capsys)


def test_every_log_line_stays_one_json_object_even_when_the_message_has_newlines(
    secure_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(secure_settings)
    logging.getLogger("security.test").info('first line\nsecond line\r\n{"forged": true}')
    lines = [line for line in output(capsys).splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["message"].startswith("first line")


def test_a_log_message_cannot_forge_another_log_record(
    secure_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(secure_settings)
    forged = '", "level": "CRITICAL", "message": "forged'
    logging.getLogger("security.test").info("user supplied %s", forged)
    records = [json.loads(line) for line in output(capsys).splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["level"] == "INFO"


def test_a_valid_key_is_not_repeated_when_another_setting_is_wrong(
    monkeypatch: pytest.MonkeyPatch, key_entry: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DOMAIN", "example.test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://example.test")
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    monkeypatch.setenv("ENCRYPTION_KEYS", key_entry)
    clear_settings_cache()
    with pytest.raises(SettingsError) as raised:
        get_settings()
    text = str(raised.value) + repr(raised.value) + str(raised.value.__cause__)
    assert key_of(key_entry) not in text
    assert "LOG_LEVEL" in text


@pytest.mark.parametrize(
    "bad",
    [
        "sec1:{key}x",  # too long for 32 bytes
        "SEC1:{key}",  # upper case id
        "sec1:{key},sec1:{key}",  # repeated id
        "{key}",  # no id
        "sec1 {key}",  # wrong separator
    ],
)
def test_a_malformed_key_list_is_rejected_without_repeating_any_part_of_it(
    monkeypatch: pytest.MonkeyPatch, key_entry: str, bad: str
) -> None:
    key = key_of(key_entry)
    monkeypatch.setenv("ENCRYPTION_KEYS", bad.format(key=key))
    clear_settings_cache()
    with pytest.raises(SettingsError) as raised:
        get_settings()
    text = str(raised.value) + repr(raised.value) + str(raised.value.__cause__)
    assert key not in text


def test_settings_never_show_keys_in_any_representation(secure_settings: Settings) -> None:
    key = key_of(secure_settings.encryption_keys.get_secret_value())
    for rendering in (
        str(secure_settings),
        repr(secure_settings),
        secure_settings.model_dump_json(),
        str(secure_settings.model_dump()),
        repr(secure_settings.encryption_keys),
    ):
        assert key not in rendering
