"""The authorization endpoint: `GET /oauth/authorize` (request, consent page) and the consent POST.

Order of checks:

1. The client and the redirect URI. If either is unknown or does not match, the person sees an
   error page and is never redirected anywhere: the address cannot be trusted yet.
2. Not signed in: to the sign-in page and back, before anything else is judged. A visitor who has
   not signed in is never sent on to the client's address.
3. Everything else (response type, PKCE, resource, state). A mistake here shows an error page that
   names the return host and offers a "Return to <host>" link carrying `error`,
   `error_description`, `state` and `iss` (RFC 6749 section 4.1.2.1, RFC 9207). The browser is
   never redirected automatically for a request error: a client could otherwise use this server
   as an open redirector for the address it registered (RFC 9700 section 4.11.2).

The consent page lets the person pick the PMS connections the assistant may use. Its
Content-Security-Policy allows forms to be sent to this site and to the exact origin of the
redirect URI (and nothing else), because browsers also apply `form-action` to the redirect that
answers a form. Redirects happen automatically only after the person acts: approving creates a
grant and a single-use code and redirects with `code`, `state` and `iss`; cancelling redirects with
`error=access_denied`.

PKCE with `S256` is mandatory (`plain` is refused). Only the scope `mcp` exists: scopes the
client asks for that are not known are ignored and the grant records `mcp`.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Final
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts.auth import (
    CurrentUser,
    PageError,
    current_user_or_none,
    first_values,
    read_form_pairs,
    require_user,
    safe_next,
    verify_csrf,
)
from pmsbridge.accounts.pages import PAGE_CSP, render
from pmsbridge.oauth import clients, consent, grants
from pmsbridge.oauth.config import AUTHORIZE_PATH, SCOPE, is_mcp_resource, issuer, mcp_resource
from pmsbridge.oauth.db import OAuthClient
from pmsbridge.oauth.redirects import (
    LOOPBACK_HOSTS,
    redirect_host,
    redirect_matches,
    redirect_origin,
)

logger = logging.getLogger("pmsbridge.oauth")

MAX_STATE_LENGTH: Final = 512
MAX_QUERY_LENGTH: Final = 1900
_CODE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_RETRY = "Go back to the assistant and start the connection again."


@dataclass(frozen=True)
class AuthorizeRequest:
    """A validated authorization request."""

    client_id: str
    client_name: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    resource: str


class AuthorizationError(Exception):
    """An error to send back to the client's redirect URI (the redirect URI is already trusted)."""

    def __init__(self, error: str, description: str, redirect_uri: str, state: str | None) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect_uri = redirect_uri
        self.state = state


def _redirect_to(uri: str, params: dict[str, str]) -> Response:
    separator = "&" if "?" in uri else "?"
    return RedirectResponse(f"{uri}{separator}{urlencode({**params, 'iss': issuer()})}", 303)


def _error_params(error: AuthorizationError) -> dict[str, str]:
    params = {"error": error.error, "error_description": error.description}
    if error.state is not None:
        params["state"] = error.state
    return params


def _error_redirect(error: AuthorizationError) -> Response:
    """The automatic redirect, used only when the person cancelled."""
    logger.info("assistant authorization refused", extra={"oauth_error": error.error})
    return _redirect_to(error.redirect_uri, _error_params(error))


def _error_page(request: Request, error: AuthorizationError) -> Response:
    """Show a request error with a link back to the client; the person decides whether to go."""
    host = redirect_host(error.redirect_uri)
    separator = "&" if "?" in error.redirect_uri else "?"
    logger.info("assistant request refused", extra={"oauth_error": error.error})
    return render(
        request,
        "oauth/request_error.html",
        status_code=400,
        description=error.description,
        redirect_host=host,
        redirect_uri=error.redirect_uri,
        on_this_computer=host.strip("[]") in LOOPBACK_HOSTS,
        return_url=(
            f"{error.redirect_uri}{separator}{urlencode({**_error_params(error), 'iss': issuer()})}"
        ),
    )


async def check_client(params: Mapping[str, str], repeated: Iterable[str] = ()) -> OAuthClient:
    """Step 1: the client and redirect URI. Raises `PageError`; nothing here redirects."""
    repeated = set(repeated)
    client = await clients.get_client(params.get("client_id", ""))
    if client is None or "client_id" in repeated:
        raise PageError(
            400, f"This request names an application that is not registered here. {_RETRY}"
        )
    redirect_uri = params.get("redirect_uri", "")
    if "redirect_uri" in repeated or not redirect_matches(redirect_uri, client.redirect_uris):
        raise PageError(
            400,
            f"This request asks to return to an address the application did not register. {_RETRY}",
        )
    return client


def check_parameters(
    params: Mapping[str, str], repeated: Iterable[str], client: OAuthClient
) -> AuthorizeRequest:
    """Step 3: the other parameters. Raises `AuthorizationError` (shown, not redirected)."""
    redirect_uri = params["redirect_uri"]
    state = params.get("state") or None

    def refuse(error: str, description: str) -> AuthorizationError:
        return AuthorizationError(error, description, redirect_uri, state)

    if set(repeated):
        raise refuse("invalid_request", "A parameter was sent more than once.")
    if state is not None and len(state) > MAX_STATE_LENGTH:
        # The oversized value is not sent back.
        raise AuthorizationError(
            "invalid_request", "The state value is too long.", redirect_uri, None
        )
    if params.get("response_type") != "code":
        raise refuse("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge", "")
    if not _CODE_CHALLENGE.fullmatch(challenge):
        raise refuse("invalid_request", "A PKCE code_challenge is required.")
    if params.get("code_challenge_method") != "S256":
        raise refuse("invalid_request", "code_challenge_method must be S256.")
    resource = params.get("resource") or mcp_resource()
    if not is_mcp_resource(resource):
        raise refuse("invalid_target", "The resource must be this server's MCP address.")
    return AuthorizeRequest(
        client.client_id, client.client_name, redirect_uri, challenge, state, mcp_resource()
    )


def _consent_csp(redirect_uri: str) -> str:
    """The page policy with the redirect origin added to `form-action` (and nothing else)."""
    base = "form-action 'self'"
    if base not in PAGE_CSP:
        raise RuntimeError("the page policy changed shape; update the consent policy")
    return PAGE_CSP.replace(base, f"{base} {redirect_origin(redirect_uri)}")


async def _consent_page(
    request: Request,
    req: AuthorizeRequest,
    user: CurrentUser,
    *,
    error: str | None = None,
    status: int = 200,
) -> Response:
    host = redirect_host(req.redirect_uri)
    hidden = [
        ("client_id", req.client_id),
        ("redirect_uri", req.redirect_uri),
        ("response_type", "code"),
        ("code_challenge", req.code_challenge),
        ("code_challenge_method", "S256"),
        ("scope", SCOPE),
        ("resource", req.resource),
    ]
    if req.state is not None:
        hidden.append(("state", req.state))
    response = render(
        request,
        "oauth/consent.html",
        status_code=status,
        client_name=req.client_name,
        redirect_host=host,
        redirect_uri=req.redirect_uri,
        on_this_computer=host.strip("[]") in LOOPBACK_HOSTS,
        hidden=hidden,
        connections=await consent.choices_for(user.user_id),
        error=error,
        authorize_path=AUTHORIZE_PATH,
    )
    response.headers["Content-Security-Policy"] = _consent_csp(req.redirect_uri)
    return response


def register_routes(app: FastAPI) -> None:
    @app.get(AUTHORIZE_PATH, include_in_schema=False)
    async def authorize(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if len(request.url.query) > MAX_QUERY_LENGTH:
            raise PageError(400, f"This request is too long. {_RETRY}")
        pairs = request.query_params.multi_items()
        values = first_values(pairs)
        repeated = {name for name in values if sum(1 for key, _ in pairs if key == name) > 1}
        client = await check_client(values, repeated)
        user = await current_user_or_none(request)
        if user is None:
            target = safe_next(f"{request.url.path}?{request.url.query}")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", 303)
        try:
            req = check_parameters(values, repeated, client)
        except AuthorizationError as error:
            return _error_page(request, error)
        return await _consent_page(request, req, user)

    @app.post(AUTHORIZE_PATH, include_in_schema=False)
    async def decide(  # pyright: ignore[reportUnusedFunction]
        request: Request, user: Annotated[CurrentUser, Depends(require_user)]
    ) -> Response:
        pairs = await read_form_pairs(request)
        form = first_values(pairs)
        await verify_csrf(request, form)
        repeated = {
            name
            for name in ("client_id", "redirect_uri", "state", "resource", "code_challenge")
            if sum(1 for key, _ in pairs if key == name) > 1
        }
        client = await check_client(form, repeated)
        try:
            req = check_parameters(form, repeated, client)
        except AuthorizationError as error:
            return _error_page(request, error)
        decision = form.get("decision")
        if decision == "deny":
            return _error_redirect(
                AuthorizationError(
                    "access_denied", "The person did not allow access.", req.redirect_uri, req.state
                )
            )
        if decision != "approve":
            raise PageError(400, "The form could not be read. Reload the page and try again.")
        selection = consent.read_selection(await consent.choices_for(user.user_id), pairs)
        if selection is None:
            return await _consent_page(
                request,
                req,
                user,
                error="Choose which connections the assistant may use.",
                status=400,
            )
        try:
            code = await grants.create_grant_and_code(
                user.user_id,
                grants.CodeRequest(
                    req.client_id, req.redirect_uri, req.code_challenge, req.resource, SCOPE
                ),
                selection,
            )
        except grants.ClientGoneError:
            raise PageError(400, f"This application's registration has expired. {_RETRY}") from None
        logger.info(
            "assistant authorized", extra={"user_id": user.user_id, "client_id": req.client_id}
        )
        params = {"code": code}
        if req.state is not None:
            params["state"] = req.state
        return _redirect_to(req.redirect_uri, params)
