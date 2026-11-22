import json
import logging
import sys

import pytest
from pydantic import SecretStr

from pmsbridge.core.logging import (
    REDACTED,
    configure_logging,
    is_sensitive_key,
    redact,
    request_id_var,
    scrub_text,
)
from pmsbridge.core.settings import Settings

pytestmark = pytest.mark.usefixtures("restore_logging")


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_each_record_is_one_json_object_with_the_core_fields(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    logging.getLogger("pmsbridge.test").info("hello %s", "world", extra={"connection_id": 7})
    (entry,) = lines(capsys)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "pmsbridge.test"
    assert entry["message"] == "hello world"
    assert entry["connection_id"] == 7
    assert "request_id" not in entry
    assert str(entry["time"]).endswith("+00:00")


def test_request_id_is_included_when_present(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    log = logging.getLogger("pmsbridge.test")
    token = request_id_var.set("req-123")
    try:
        log.info("inside a request")
    finally:
        request_id_var.reset(token)
    log.info("explicit", extra={"request_id": "req-456"})
    first, second = lines(capsys)
    assert first["request_id"] == "req-123"
    assert second["request_id"] == "req-456"


def test_level_comes_from_the_settings(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(environment="development", log_level="WARNING"))
    log = logging.getLogger("pmsbridge.test")
    log.info("hidden")
    log.warning("shown")
    assert [e["message"] for e in lines(capsys)] == ["shown"]


def test_configure_logging_replaces_existing_handlers_and_is_repeatable(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    logging.basicConfig(stream=sys.stdout, format="PLAIN %(message)s")
    configure_logging(dev_settings)
    configure_logging(dev_settings)
    assert len(logging.getLogger().handlers) == 1
    logging.getLogger("pmsbridge.test").info("once")
    assert len(lines(capsys)) == 1


def test_secret_keys_are_redacted_at_any_depth_and_case(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    logging.getLogger("pmsbridge.test").info(
        "request done",
        extra={
            "Password": "hunter2",
            "payload": {
                "Client_Secret": "s3cr3t",
                "items": [{"access_token": "tok-1"}, {"harmless": "visible"}],
                "headers": {"Authorization": "Bearer abc.def.ghi", "Set-Cookie": "sid=1"},
            },
            "contact": {"EMAIL": "guest@example.com", "phone_number": "+15550100"},
            "api_key": "k-1",
            "note": "kept",
        },
    )
    (entry,) = lines(capsys)
    text = json.dumps(entry)
    for leaked in (
        "hunter2",
        "s3cr3t",
        "tok-1",
        "abc.def.ghi",
        "sid=1",
        "guest@",
        "+15550100",
        "k-1",
    ):
        assert leaked not in text
    assert entry["Password"] == REDACTED
    payload = entry["payload"]
    assert isinstance(payload, dict)
    assert payload["Client_Secret"] == REDACTED
    assert payload["items"][1] == {"harmless": "visible"}
    assert entry["note"] == "kept"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "x-api-key",
        "apiKey",
        "refresh_token",
        "Authorization",
        "Cookie",
        "user_email",
        "phone",
        "client_secret",
    ],
)
def test_sensitive_key_detection(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["status", "connection_id", "duration_ms", "path"])
def test_ordinary_keys_are_kept(key: str) -> None:
    assert not is_sensitive_key(key)


def test_secrets_in_message_text_are_masked(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    log = logging.getLogger("pmsbridge.test")
    log.info("login failed for guest@example.com with password=hunter2")
    log.info("calling with Authorization: Bearer abcdef123456 now")
    log.info("body %s", '{"client_secret": "abc123", "ok": true}')
    log.info("token refreshed for connection 5")
    messages = [str(e["message"]) for e in lines(capsys)]
    assert messages[0] == f"login failed for {REDACTED} with password={REDACTED}"
    assert messages[1] == f"calling with Authorization: {REDACTED}"
    assert "abc123" not in messages[2]
    assert '"ok": true' in messages[2]
    assert messages[3] == "token refreshed for connection 5"


def test_quoted_values_and_cookie_headers_are_masked_to_the_end(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    log = logging.getLogger("pmsbridge.test")
    log.info('retry with password="correct horse battery" and more')
    log.info("sent Cookie: a=1; session=xyz")
    log.info("got Set-Cookie: sid=9; Path=/; HttpOnly\nnext line stays")
    log.info("config {'client_secret': 'two words', 'mode': 'x'}")
    messages = [str(e["message"]) for e in lines(capsys)]
    assert messages[0] == f"retry with password={REDACTED} and more"
    assert messages[1] == f"sent Cookie: {REDACTED}"
    assert messages[2] == f"got Set-Cookie: {REDACTED}\nnext line stays"
    assert "two words" not in messages[3]
    assert "'mode': 'x'" in messages[3]


def test_exact_safe_keys_are_kept_but_token_keys_stay_masked(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    logging.getLogger("pmsbridge.test").info(
        "budget token_budget_remaining=3 token=abc access_token=def",
        extra={
            "token_budget_remaining": 3,
            "Token_Budget_Reset_Seconds": 120,
            "token_requests_today": 2,
            "token": "abc",
            "access_token": "def",
            "token_budget": "close-but-not-listed",
        },
    )
    (entry,) = lines(capsys)
    assert (
        entry["message"]
        == f"budget token_budget_remaining=3 token={REDACTED} access_token={REDACTED}"
    )
    assert entry["token_budget_remaining"] == 3
    assert entry["Token_Budget_Reset_Seconds"] == 120
    assert entry["token_requests_today"] == 2
    assert entry["token"] == REDACTED
    assert entry["access_token"] == REDACTED
    assert entry["token_budget"] == REDACTED
    assert not is_sensitive_key("token_budget_remaining")


def test_unformattable_messages_do_not_break_logging(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    log = logging.getLogger("pmsbridge.test")
    log.info("needs %s and %s password=hunter2", "one")  # noqa: PLE1206
    log.info("bad %d", "text")
    first, second = lines(capsys)
    assert str(first["message"]).endswith(" [unformattable args]")
    assert "hunter2" not in str(first["message"])
    assert str(second["message"]).endswith(" [unformattable args]")


def test_exception_tracebacks_are_included_and_scrubbed(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    try:
        raise ValueError("bad credentials: password=hunter2 for guest@example.com")
    except ValueError:
        logging.getLogger("pmsbridge.test").exception("operation failed")
    (entry,) = lines(capsys)
    exception = str(entry["exception"])
    assert "ValueError" in exception
    assert "hunter2" not in exception
    assert "guest@example.com" not in exception


def test_secret_str_objects_and_odd_values_do_not_break_or_leak(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)

    class Thing:
        def __repr__(self) -> str:
            return "Thing(token=abc)"

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    logging.getLogger("pmsbridge.test").info(
        "values",
        extra={
            "wrapped": SecretStr("hidden-value"),
            "thing": Thing(),
            "raw": b"\x00\x01",
            "loop": cyclic,
        },
    )
    (entry,) = lines(capsys)
    text = json.dumps(entry)
    assert "hidden-value" not in text
    assert "token=abc" not in text
    assert entry["raw"] == "[2 bytes]"


def test_redact_walks_without_mutating_the_input() -> None:
    inner = {"password": "p", "keep": [1, 2]}
    original = {"outer": inner}
    result = redact(original)
    assert inner["password"] == "p"  # noqa: S105
    assert result == {"outer": {"password": REDACTED, "keep": [1, 2]}}


def test_very_long_text_is_truncated_quickly() -> None:
    result = scrub_text("a" * 1_000_000)
    assert len(result) < 9000
    assert result.endswith("[truncated]")
