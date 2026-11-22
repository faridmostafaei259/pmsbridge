import pytest
from starlette.requests import Request

from pmsbridge.accounts import csrf


def request_with(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_a_token_matches_its_secret_and_differs_every_time() -> None:
    secret = csrf.new_secret()
    first, second = csrf.mask_token(secret), csrf.mask_token(secret)
    assert first != second
    assert csrf.token_matches(secret, first)
    assert csrf.token_matches(secret, second)


def test_a_token_does_not_contain_the_secret() -> None:
    secret = csrf.new_secret()
    assert secret not in csrf.mask_token(secret)


@pytest.mark.parametrize("token", [None, "", "x", "!!!!", "A" * 86, "A" * 200])
def test_wrong_or_malformed_tokens_are_refused(token: str | None) -> None:
    assert not csrf.token_matches(csrf.new_secret(), token)


def test_a_token_for_another_secret_is_refused() -> None:
    assert not csrf.token_matches(csrf.new_secret(), csrf.mask_token(csrf.new_secret()))


def test_a_damaged_stored_secret_is_refused_without_an_error() -> None:
    assert not csrf.token_matches("not hex", csrf.mask_token(csrf.new_secret()))


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://pms.example.test", True),
        ("https://PMS.example.test", True),
        ("https://pms.example.test:443", True),
        ("http://pms.example.test", False),
        ("https://evil.example", False),
        ("https://pms.example.test.evil.example", False),
        ("https://pms.example.test:8443", False),
        ("null", False),
        ("", False),
    ],
)
def test_origin_header_must_match_the_public_address(origin: str, allowed: bool) -> None:
    request = request_with({"Origin": origin})
    assert csrf.origin_allowed(request, "https://pms.example.test") is allowed


def test_referer_is_used_only_when_origin_is_absent() -> None:
    base = "https://pms.example.test"
    assert csrf.origin_allowed(request_with({"Referer": f"{base}/login"}), base)
    assert not csrf.origin_allowed(request_with({"Referer": "https://evil.example/login"}), base)
    # A wrong Origin is not rescued by a good Referer.
    both = request_with({"Origin": "https://evil.example", "Referer": f"{base}/login"})
    assert not csrf.origin_allowed(both, base)


def test_a_request_with_neither_origin_nor_referer_is_refused() -> None:
    assert not csrf.origin_allowed(request_with({}), "https://pms.example.test")
