import httpx
from oauth_helpers import BASE_URL, Browser


async def test_authorization_server_metadata_lists_the_endpoints(
    anonymous: httpx.AsyncClient,
) -> None:
    response = await anonymous.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == BASE_URL
    assert body["authorization_endpoint"] == f"{BASE_URL}/oauth/authorize"
    assert body["token_endpoint"] == f"{BASE_URL}/oauth/token"
    assert body["registration_endpoint"] == f"{BASE_URL}/oauth/register"
    assert body["response_types_supported"] == ["code"]
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert set(body["token_endpoint_auth_methods_supported"]) == {
        "none",
        "client_secret_post",
        "client_secret_basic",
    }
    assert body["authorization_response_iss_parameter_supported"] is True


async def test_protected_resource_metadata_names_the_mcp_endpoint_and_the_issuer(
    anonymous: httpx.AsyncClient,
) -> None:
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        response = await anonymous.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["resource"] == f"{BASE_URL}/mcp"
        assert body["authorization_servers"] == [BASE_URL]
        assert body["bearer_methods_supported"] == ["header"]


async def test_other_resource_paths_are_not_served(anonymous: httpx.AsyncClient) -> None:
    response = await anonymous.get("/.well-known/oauth-protected-resource/elsewhere")
    assert response.status_code == 404


async def test_metadata_ignores_host_and_forwarding_headers(make_browser: Browser) -> None:
    client = make_browser("203.0.113.5")
    headers = {
        "Host": "evil.example",
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-Proto": "http",
        "Forwarded": "host=evil.example;proto=http",
    }
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        text = (await client.get(path, headers=headers)).text
        assert "evil.example" not in text
        assert BASE_URL in text


async def test_metadata_is_public_cacheable_and_sets_no_cookies(
    anonymous: httpx.AsyncClient,
) -> None:
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        response = await anonymous.get(path)
        assert response.headers["cache-control"] == "public, max-age=300"
        assert "set-cookie" not in response.headers
        assert response.headers["access-control-allow-origin"] == "*"


async def test_metadata_answers_a_browser_preflight(anonymous: httpx.AsyncClient) -> None:
    response = await anonymous.options(
        "/.well-known/oauth-protected-resource",
        headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "GET" in response.headers["access-control-allow-methods"]


async def test_registration_has_no_cors_opening(anonymous: httpx.AsyncClient) -> None:
    response = await anonymous.post(
        "/oauth/register",
        json={"client_name": "x", "redirect_uris": ["https://a.example/cb"]},
        headers={"Origin": "https://app.example"},
    )
    assert "access-control-allow-origin" not in response.headers
