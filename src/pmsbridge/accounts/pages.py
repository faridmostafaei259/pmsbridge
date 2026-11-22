"""HTML rendering for the dashboard: Jinja2 with autoescape and strict headers."""

from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.requests import Request
from starlette.responses import HTMLResponse

from pmsbridge.accounts.auth import CurrentUser, get_csrf_token

PACKAGE_DIR: Final = Path(__file__).resolve().parent.parent
TEMPLATE_DIR: Final = PACKAGE_DIR / "templates"
STATIC_DIR: Final = PACKAGE_DIR / "static"

# Styles only from our own stylesheet and no scripts at all. Forms may post only to this site.
PAGE_CSP: Final = (
    "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# `no-transform` asks proxies not to compress the page. Pages with a CSRF token should not be
# compressed (BREACH); tokens are also masked per response, so this is a second layer.
PAGE_CACHE_CONTROL: Final = "no-store, no-transform"
# The edge default (`no-referrer`) makes browsers send `Origin: null` with form posts, which the
# Origin check must refuse. `same-origin` sends the real Origin to this site and nothing elsewhere.
PAGE_REFERRER_POLICY: Final = "same-origin"

environment = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(
    request: Request, template: str, *, status_code: int = 200, **context: object
) -> HTMLResponse:
    """Render a template. `user` and `csrf_token` are added when the request has a session."""
    state = request.state
    info = getattr(state, "session_info", None)
    user = None if info is None else CurrentUser(info.user_id, info.email, info.role)
    has_secret = isinstance(getattr(state, "csrf_secret", None), str)
    page = environment.get_template(template).render(
        user=user, csrf_token=get_csrf_token(request) if has_secret else None, **context
    )
    return HTMLResponse(
        page,
        status_code=status_code,
        headers={
            "Content-Security-Policy": PAGE_CSP,
            "Cache-Control": PAGE_CACHE_CONTROL,
            "Referrer-Policy": PAGE_REFERRER_POLICY,
        },
    )
