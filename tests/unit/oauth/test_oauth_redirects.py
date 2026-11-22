import pytest

from pmsbridge.oauth.redirects import (
    InvalidRedirectError,
    redirect_host,
    redirect_matches,
    redirect_origin,
    validate_redirect_uri,
)


@pytest.mark.parametrize(
    "uri",
    [
        "https://assistant.example.test/callback",
        "https://assistant.example.test:8443/cb?x=1",
        "http://127.0.0.1:53682/callback",
        "http://[::1]:7000/cb",
        "http://localhost:3000/oauth/callback",
        "http://localhost/cb",
    ],
)
def test_good_redirect_uris_are_accepted(uri: str) -> None:
    assert validate_redirect_uri(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "http://assistant.example.test/callback",
        "http://127.0.0.1.example.test/cb",
        "ftp://assistant.example.test/cb",
        "javascript:alert(1)",
        "myapp://callback",
        "//assistant.example.test/cb",
        "/relative",
        "https://assistant.example.test/cb#frag",
        "https://user:pass@assistant.example.test/cb",
        "https://assistant.example.test@evil.example/cb",
        "https://assistant.example.test:0/cb",
        "https://assistant.example.test:99999/cb",
        "https://assistant example.test/cb",
        "https://assistant.example.test\\evil.example/cb",
        "https://assistant.example.test/cb\r\nX: y",
        "https://exa;mple.test/cb",
        "http://[fe80::1%25eth0]/cb",
        "https://[fe80::1%eth0]/cb",
        "https://exa%6dple.test/cb",
        "https://bücher.example/cb",
        "https://" + "a" * 3000 + ".example/cb",
    ],
)
def test_bad_redirect_uris_are_refused(uri: str) -> None:
    with pytest.raises(InvalidRedirectError):
        validate_redirect_uri(uri)


def test_a_redirect_uri_must_be_text() -> None:
    for value in (None, 5, ["https://a.example/cb"], {"a": 1}):
        with pytest.raises(InvalidRedirectError):
            validate_redirect_uri(value)


def test_matching_is_exact_for_ordinary_addresses() -> None:
    registered = ["https://assistant.example.test/callback"]
    assert redirect_matches("https://assistant.example.test/callback", registered)
    for other in (
        "https://assistant.example.test/callback/",
        "https://assistant.example.test/callback?x=1",
        "https://assistant.example.test:444/callback",
        "https://Assistant.example.test/callback",
        "https://evil.example/callback",
        "https://assistant.example.test/callback2",
    ):
        assert not redirect_matches(other, registered)


def test_loopback_addresses_may_use_another_port_but_nothing_else_may_differ() -> None:
    registered = ["http://127.0.0.1:5000/cb", "http://[::1]/cb"]
    assert redirect_matches("http://127.0.0.1:61234/cb", registered)
    assert redirect_matches("http://[::1]:61234/cb", registered)
    assert not redirect_matches("http://127.0.0.1:61234/other", registered)
    assert not redirect_matches("http://127.0.0.1:61234/cb?x=1", registered)
    assert not redirect_matches("https://127.0.0.1:61234/cb", registered)
    assert not redirect_matches("http://localhost:61234/cb", registered)


def test_localhost_by_name_matches_exactly() -> None:
    registered = ["http://localhost:3000/cb"]
    assert redirect_matches("http://localhost:3000/cb", registered)
    assert not redirect_matches("http://localhost:3001/cb", registered)


def test_a_port_change_cannot_move_a_registered_address_to_another_host() -> None:
    assert not redirect_matches("http://127.0.0.1@evil.example:80/cb", ["http://127.0.0.1:5000/cb"])


def test_origin_and_host_for_the_consent_page() -> None:
    assert redirect_origin("https://a.example.test/cb?x=1") == "https://a.example.test"
    assert redirect_origin("http://127.0.0.1:53682/cb") == "http://127.0.0.1:53682"
    assert redirect_origin("http://[::1]:7000/cb") == "http://[::1]:7000"
    assert redirect_host("http://[::1]:7000/cb") == "[::1]"
    assert redirect_host("https://a.example.test:8443/cb") == "a.example.test"
