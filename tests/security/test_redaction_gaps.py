"""Redaction behaviour: the common shapes, and the gaps found by the security audit (now closed).

Every case here must always pass. The second group holds the audit's confirmed gaps; they were
`xfail(strict=True)` until the redaction was fixed, and are normal regression tests now.
"""

import base64
import json
import re

import pytest

from pmsbridge.core.logging import REDACTED, redact, scrub_text

MARKER = "Zq9-sentinel-value-4711"

# ---- Enforced today ----


@pytest.mark.parametrize(
    "text",
    [
        "password={m}",
        "PASSWORD={m}",
        "password: {m}",
        "password : {m}",
        '"password": "{m}"',
        "'password': '{m}'",
        "{{'api_key': '{m}'}}",
        "client_secret={m}&other=1",
        "https://host/cb?access_token={m}&state=1",
        "https://host/cb#access_token={m}&token_type=bearer",
        "x-auth-token={m}",
        "Authorization: Bearer {m}",
        "authorization: bearer {m}",
        "request sent with Bearer {m} to host",
        "Authorization: Basic {m}==",
        "Cookie: a=1; sid={m}",
        "Set-Cookie: sid={m}; HttpOnly; Secure",
        'password="{m} with spaces"',
        "refresh_token={m}",
        "credentials={m}",
        "private_key={m}",
        "line one\npassword={m}",
        "x" * 30 + "password={m}",
    ],
)
def test_common_secret_shapes_are_masked_in_free_text(text: str) -> None:
    assert MARKER not in scrub_text(text.format(m=MARKER))


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "client_secret",
        "access_token",
        "refresh-token",
        "authorization",
        "api_key",
        "apiKey",
        "cookie",
        "set-cookie",
        "email",
        "guest_email",
        "phone",
        "credentials",
        "private_key",
        "encryption_keys",
    ],
)
def test_sensitive_mapping_keys_are_masked_at_any_depth(key: str) -> None:
    result = redact({"outer": [{"inner": {key: MARKER}}]})
    assert MARKER not in json.dumps(result)
    assert REDACTED in json.dumps(result)


def test_email_addresses_are_masked() -> None:
    assert "guest@example.com" not in scrub_text("sent to guest@example.com today")
    assert "a.b+c@sub.example.co.uk" not in scrub_text("a.b+c@sub.example.co.uk")


def test_masking_is_fast_on_hostile_input() -> None:
    hostile = ("password=" * 4000) + ("a" * 100000)
    scrub_text(hostile)  # must return; the length cap bounds the work
    scrub_text("x" * 100000 + "=" * 5000)
    scrub_text(("password:" + " " * 500) * 20)


# ---- Gaps found by the security audit (closed) ----
#
# Each case is its own `pytest.param`. Mapping keys (`redact`) and free text (`scrub_text`) are
# asserted separately.


def gap(*values: str) -> list[object]:
    return [pytest.param(value, id=value[:40]) for value in values]


@pytest.mark.parametrize(
    "text",
    gap(
        json.dumps(json.dumps({"password": MARKER})),
        json.dumps({"detail": json.dumps({"client_secret": MARKER})}),
        'upstream said {\\"access_token\\": \\"' + MARKER + '\\"}',
    ),
)
def test_gap_secrets_inside_escaped_json_are_masked(text: str) -> None:
    assert MARKER not in scrub_text(text)


@pytest.mark.parametrize(
    "text",
    gap(
        'x {"password": "ab\\"cd' + MARKER + '"}',
        'password="ab\\"' + MARKER + '"',
        "password='ab\\'" + MARKER + "'",
    ),
)
def test_gap_an_escaped_quote_inside_a_quoted_value_does_not_leak_the_tail(text: str) -> None:
    assert MARKER not in scrub_text(text)


@pytest.mark.parametrize(
    "text",
    gap(
        f"password%3D{MARKER}",
        f"access_token%3D{MARKER}%26state%3D1",
        f"https://host/cb?client_secret%3D{MARKER}",
    ),
)
def test_gap_url_encoded_pairs_are_masked(text: str) -> None:
    assert MARKER not in scrub_text(text)


# Key names that carry credentials but are not on the sensitive list.
GAP_KEYS = (
    "session",
    "sessionid",
    "sid",
    "csrf",
    "code",
    "code_verifier",
    "totp",
    "otp",
    "mfa_code",
    "recovery_code",
    "assertion",
    "client_assertion",
    "jwt",
    "signature",
    "hmac",
    "signing_key",
    "pwd",
    "passcode",
)


@pytest.mark.parametrize("key", gap(*GAP_KEYS))
def test_gap_more_secret_key_names_are_masked_in_mappings(key: str) -> None:
    assert MARKER not in json.dumps(redact({key: MARKER}))


@pytest.mark.parametrize("key", gap(*GAP_KEYS))
def test_gap_more_secret_key_names_are_masked_in_text(key: str) -> None:
    assert MARKER not in scrub_text(f"{key}={MARKER}")


def test_gap_an_oauth_code_in_a_query_string_is_masked() -> None:
    assert MARKER not in scrub_text(f"GET /authorize/callback?code={MARKER}&state=abc")


def _b64url(data: dict[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_gap_a_bare_jwt_in_text_is_masked() -> None:
    # Built at run time so that no token-shaped string exists in the source tree.
    signature = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    jwt = f"{_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_b64url({'sub': 'user-1'})}.{signature}"
    assert jwt not in scrub_text(f"token was {jwt} for user")


def test_gap_a_key_entry_after_a_word_character_is_masked() -> None:
    entry = "k_sec1:" + "A" * 43 + "="
    assert not re.search(r"A{43}", scrub_text(entry))
