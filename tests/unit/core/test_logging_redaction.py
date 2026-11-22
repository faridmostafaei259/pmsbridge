"""Word-aware key matching, nested and encoded pairs, and containment of failing log calls."""

import base64
import json
import logging
import time

import pytest

from pmsbridge.core.logging import REDACTED, configure_logging, is_sensitive_key, redact, scrub_text
from pmsbridge.core.settings import Settings

pytestmark = pytest.mark.usefixtures("restore_logging")

MARKER = "Zq9-sentinel-value-4711"

ORDINARY_WORDS = [
    "footprint",
    "hotplate",
    "postcode",
    "encode",
    "decode",
    "barcode",
    "unicode",
    "inside",
    "consider",
    "residence",
    "possession",
    "obsession",
    "sessions_active",
    "status",
    "pwdless",
]


@pytest.mark.parametrize("word", ORDINARY_WORDS)
def test_ordinary_words_are_not_sensitive_keys(word: str) -> None:
    assert not is_sensitive_key(word)
    assert redact({word: "visible"}) == {word: "visible"}


@pytest.mark.parametrize("word", ORDINARY_WORDS)
def test_ordinary_words_are_not_masked_in_text(word: str) -> None:
    text = f"{word}=visible and {word}: visible and ?{word}=visible&x=1"
    assert scrub_text(text) == text


@pytest.mark.parametrize(
    "key",
    [
        "code",
        "Code",
        "auth_code",
        "authorization-code",
        "mfaCode",
        "oauth.code",
        "sid",
        "connect.sid",
        "userSid",
        "OTP",
        "otp_value",
        "pwd",
        "newPwd",
        "session",
        "session_cookie",
        "sessionId",
        "PHPSESSID",
        "codeVerifier",
        "X-CSRF-Token",
        "x-xsrf-token",
        "clientAssertion",
        "SIGNATURE",
    ],
)
def test_sensitive_names_are_found_in_any_spelling(key: str) -> None:
    assert is_sensitive_key(key)
    assert MARKER not in json.dumps(redact({key: MARKER}))
    assert MARKER not in scrub_text(f"{key}={MARKER}")


@pytest.mark.parametrize(
    "key",
    [
        "status_code",
        "http_status_code",
        "error_code",
        "exit_code",
        "country_code",
        "currency_code",
    ],
)
def test_status_and_locale_codes_are_kept(key: str) -> None:
    assert not is_sensitive_key(key)
    assert scrub_text(f"{key}=404") == f"{key}=404"
    assert redact({key: 404}) == {key: 404}


def test_a_pair_inside_the_value_of_another_pair_is_still_masked() -> None:
    assert MARKER not in scrub_text(f"redirect_uri=https://host/cb?code={MARKER}&state=1")
    assert MARKER not in scrub_text(f"url=https://host/?access_token={MARKER}")
    assert MARKER not in scrub_text(f"a=password={MARKER}")
    assert scrub_text("a=1 b=2") == "a=1 b=2"


def test_state_and_other_query_values_next_to_a_code_are_kept() -> None:
    result = scrub_text(f"GET /cb?code={MARKER}&state=abc&scope=read")
    assert result == f"GET /cb?code={REDACTED}&state=abc&scope=read"


@pytest.mark.parametrize(
    "text",
    [
        f"?a=1%26code%3D{MARKER}%26b%3D2",
        f"x%3Fsession%3A{MARKER}",
        f"password%3D%22{MARKER}%22",
        f"PASSWORD%3d{MARKER}",
    ],
)
def test_url_encoded_pairs_are_masked_after_other_encoded_text(text: str) -> None:
    result = scrub_text(text)
    assert MARKER not in result


def test_the_text_around_an_encoded_pair_is_kept() -> None:
    result = scrub_text(f"access_token%3D{MARKER}%26state%3D1")
    assert result == f"access_token%3D{REDACTED}%26state%3D1"


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(json.dumps(json.dumps({"password": MARKER}))),
        'x {\\"password\\": \\"a\\\\\\"b ' + MARKER + '\\"} y',
        'password="never closed ' + MARKER,
        "password='never closed " + MARKER,
        '{"note": "a", "client_secret": "q\\"r ' + MARKER + '", "k": 1}',
    ],
)
def test_escaped_unterminated_and_nested_values_leave_no_tail(text: str) -> None:
    assert MARKER not in scrub_text(text)


def test_values_after_a_masked_quoted_value_are_kept() -> None:
    result = scrub_text('{"password": "a\\"b", "mode": "x"}')
    assert result == f'{{"password": {REDACTED}, "mode": "x"}}'


def _jwt(parts: int = 3) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    segments = [
        b64(b'{"alg":"HS256","typ":"JWT"}'),
        b64(b'{"sub":"user-1"}'),
        b64(bytes(range(32))),
    ]
    return ".".join(segments[:parts])


def test_a_jwt_is_masked_wherever_it_appears() -> None:
    jwt = _jwt()
    for text in (
        f"got {jwt}",
        f"got {jwt}.",
        f"[{jwt}]",
        f'"{jwt}"',
        f"x?a={jwt}&b=1",
        f"Bearer {jwt}",
    ):
        assert jwt not in scrub_text(text)
    assert scrub_text(f"got {jwt} ok") == f"got {REDACTED} ok"


@pytest.mark.parametrize(
    "text",
    ["eyJ", "eyJhbGci", "see eyJhbGciOi.only", "version 1.2.3", "host.example.com", "a.b.c"],
)
def test_text_that_only_looks_a_little_like_a_jwt_is_kept(text: str) -> None:
    assert scrub_text(text) == text


def test_a_key_entry_is_masked_when_glued_to_other_characters() -> None:
    body = "A" * 43 + "="
    for prefix in ("k_sec1:", "Xsec1:", "id=sec1:", "-sec1:"):
        assert "A" * 43 not in scrub_text(prefix + body)
    assert "A" * 43 not in scrub_text("x" + "sec1:" + body + "tail")


def test_ordinary_colons_and_base64_are_not_mistaken_for_a_key_entry() -> None:
    text = "time 12:30:45 ratio 3:4 path a:b value " + "A" * 43
    assert scrub_text(text) == text


def test_hostile_inputs_stay_fast() -> None:
    inputs = [
        'password="' * 800,
        "password=\\\\\\" * 1500,
        "a=" * 4000,
        "a " * 4000,
        "a b c=" * 1300,
        ("x" * 79 + "=") * 100,
        "eyJ" + "a" * 7000,
        "%26" * 2500,
        "code:" + " " * 7000,
        '\\"' * 4000 + 'password:\\"' * 100,
        'password=\\"x ' * 600,
        'secret=\\"a\\"b ' * 600,
        "token=\\'a\\\\'b " * 600,
        "Session ID " * 700,
        "password='" + "\\\\\\'x" * 40,
        "password='" + "\\\\\\'x" * 40 + "'",
        "password='" + "\\\\\\'x" * 1500,
        "password='" + "\\\\\\'x" * 1500 + "'",
        'password="' + '\\\\\\"x' * 1500 + '"',
        'password=\\"' + '\\\\\\"x' * 1500,
        'password=\\"' + '\\\\\\"x' * 1500 + '\\"',
        "password='" + "\\" * 7000,
    ]
    for text in inputs:
        start = time.perf_counter()
        scrub_text(text)
        assert time.perf_counter() - start < 0.5, text[:30]


# ---- Escaped quotes, multi-word names, normalisation, raw header lists ----


def _scrubbed_everywhere(text: str) -> list[str]:
    """The text through scrub_text, through redact() as an extra value, and as a log message."""
    return [scrub_text(text), json.dumps(redact({"detail": text})), json.dumps(redact([text]))]


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize(
    "inner",
    [
        'login failed password="SECRETPART"',
        "login failed password='SECRETPART'",
        'login failed password="SECRET PART with spaces"',
        "retry client_secret: 'SECRET PART'",
        'body {"access_token": "SECRETPART", "ok": true}',
        "x password='it\\'s SECRETPART'",
    ],
)
def test_a_secret_in_a_json_encoded_message_is_masked(inner: str, depth: int) -> None:
    text = inner
    for _ in range(depth):
        text = json.dumps({"msg": text})
    for result in _scrubbed_everywhere(text):
        assert "SECRETPART" not in result
        assert "PART with" not in result


def test_a_json_encoded_message_keeps_its_structure_around_the_mask() -> None:
    text = json.dumps({"msg": 'login failed password="hunter2"', "level": "warn"})
    result = scrub_text(text)
    assert "hunter2" not in result
    assert result == '{"msg": "login failed password=' + REDACTED + '", "level": "warn"}'


@pytest.mark.parametrize(
    "text",
    [
        'upstream said {\\"access_token\\": \\"SECRETPART',
        'upstream said {\\"access_token\\": \\"SECRETPART and more',
        'x password="SECRETPART and more',
        "x password='SECRETPART and more",
        'x password=\\"SECRETPART',
        "x password=\\'SECRETPART",
        'x {"password": "SECRETPART',
    ],
)
def test_a_value_with_no_closing_quote_is_masked_in_full(text: str) -> None:
    for result in _scrubbed_everywhere(text):
        assert "SECRETPART" not in result
        assert "and more" not in result


def test_a_json_encoded_message_through_a_real_log_call(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    log = logging.getLogger("pmsbridge.test")
    log.info("upstream said %s", json.dumps({"msg": 'login failed password="SECRETPART"'}))
    log.info("extra", extra={"payload": json.dumps({"msg": "client_secret='SECRETPART'"})})
    assert "SECRETPART" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "text",
    [
        "Session ID: abc",
        "session id=abc",
        "API key: abc",
        "api-key: abc",
        "private key: abc",
        "signing key: abc",
        "code verifier: abc",
        "Recovery code: abc",
        "the Session ID: abc",
        "x-csrf token: abc",
    ],
)
def test_multi_word_names_in_text_are_masked(text: str) -> None:
    assert "abc" not in scrub_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "HTTP status code: 502",
        "exit code: 1",
        "error code: 429",
        "Error code=429",
        "upstream returned error code: 429",
        "country code: US",
        "my password is ok: yes",
        "the footprint size: 3",
        "postal code: 12345",
        "zip code: 12345",
        "inside the barcode: 7",
    ],
)
def test_ordinary_multi_word_names_in_text_are_kept(text: str) -> None:
    assert scrub_text(text) == text


@pytest.mark.parametrize(
    "key",
    [
        "signing.key",
        "private.key",
        "api.key",
        "backup_codes",
        "codes",
        "recoveryCodes",
        "otpcode",
        "otp_code",
        "backupcode",
        "sms_code",
        "smscode",
        "verification_code",
        "email.verification.code",
    ],
)
def test_more_spellings_are_sensitive(key: str) -> None:
    assert is_sensitive_key(key)
    assert MARKER not in json.dumps(redact({key: MARKER}))
    assert MARKER not in scrub_text(f"{key}={MARKER}")


@pytest.mark.parametrize(
    "key", ["status_codes", "http.status.codes", "codec", "decodes", "zipcodes"]
)
def test_codes_that_are_not_secrets_are_kept(key: str) -> None:
    assert not is_sensitive_key(key)


def test_raw_header_lists_are_masked() -> None:
    raw = [
        (b"host", b"example.test"),
        (b"cookie", f"sid={MARKER}".encode()),
        (b"Authorization", f"Bearer {MARKER}".encode()),
        (b"x-csrf-token", MARKER.encode()),
        (b"accept", b"*/*"),
    ]
    result = json.dumps(redact({"scope": {"headers": raw}}))
    assert MARKER not in result
    assert "[12 bytes]" in result
    assert "authorization" in result.lower()
    assert "accept" in result


@pytest.mark.parametrize(
    "headers",
    [
        [("cookie", MARKER), ("accept", "x")],
        (("Set-Cookie", MARKER), ("accept", "x")),
        [["password", MARKER], ["accept", "x"]],
        list({"X-Api-Key": MARKER, "accept": "x"}.items()),
        [(b"session", MARKER.encode())],
    ],
)
def test_pairs_in_lists_and_tuples_are_masked(headers: object) -> None:
    assert MARKER not in json.dumps(redact(headers))


@pytest.mark.parametrize(
    "data",
    [
        ["a sentence with spaces", "visible"],
        ["password is a long phrase here", "visible"],
        ["x" * 101, "visible"],
        ["pass word", "visible"],
        ["", "visible"],
        ["token", "visible", "third"],
    ],
)
def test_two_item_lists_that_do_not_start_with_a_name_are_walked_normally(
    data: list[object],
) -> None:
    assert redact(data) == data


def test_a_pair_that_is_not_a_secret_keeps_its_value_and_longer_lists_are_walked() -> None:
    assert redact([("accept", "x")]) == [["accept", "x"]]
    assert redact(("a", "b", "c")) == ["a", "b", "c"]
    assert redact([1, 2]) == [1, 2]
    assert redact([("password", MARKER, "x")]) == [["password", MARKER, "x"]]


def test_raw_header_lists_through_a_real_log_call(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    logging.getLogger("pmsbridge.test").info(
        "request", extra={"headers": [(b"cookie", MARKER.encode()), (b"accept", b"*/*")]}
    )
    assert MARKER not in capsys.readouterr().out


class _Boom:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __str__(self) -> str:
        raise self._error

    __repr__ = __str__


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("x"),
        OSError("x"),
        AttributeError("x"),
        ZeroDivisionError("x"),
        LookupError("x"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_any_error_while_rendering_is_contained_and_its_text_is_not_written(
    error: Exception, dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_text = f"hunter2-{MARKER}"
    configure_logging(dev_settings)
    log = logging.getLogger("pmsbridge.test")
    log.info("value %s password=%s", _Boom(type(error)(secret_text)), secret_text)
    log.info("after")
    first, second = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert first["message"].endswith(" [unformattable args]")
    assert MARKER not in json.dumps(first)
    assert second["message"] == "after"


def test_a_message_object_that_cannot_be_rendered_is_contained(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    logging.getLogger("pmsbridge.test").info(_Boom(RuntimeError(MARKER)))  # pyright: ignore[reportArgumentType]
    (entry,) = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert "unformattable" in entry["message"]
    assert MARKER not in json.dumps(entry)


def test_an_extra_field_that_cannot_be_rendered_is_contained(
    dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(dev_settings)
    logging.getLogger("pmsbridge.test").info(
        "with extras",
        extra={"thing": _Boom(RuntimeError(MARKER)), "nested": [_Boom(OSError(MARKER))]},
    )
    (entry,) = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert entry["message"] == "with extras"
    assert MARKER not in json.dumps(entry)
