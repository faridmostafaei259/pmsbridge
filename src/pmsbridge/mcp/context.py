"""Who is calling a tool, and which PMS connection the call is about.

A `ToolContext` is built for each call from the verified access token and nothing else: the
person, the assistant and the connections come from the token's grant, never from a tool argument.
`resolve_connection` turns the `connection` argument (an id or a label, or nothing) into one of the
allowed connections. A connection that exists but is not allowed gets exactly the same answer as
one that does not exist, so nothing about other connections can be learned.
"""

from dataclasses import dataclass
from typing import Final

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict
from sqlalchemy import select

from pmsbridge.core.storage import session
from pmsbridge.oauth.tokens import AccessPrincipal
from pmsbridge.pms.base import ConnectionRef, Provider
from pmsbridge.pms.db import PMSConnection

USABLE_STATUSES: Final = ("active", "token_budget_low")
MAX_NAMES_LISTED: Final = 20


class PrincipalToken(AccessToken):
    """The SDK's access token, carrying the checked principal for the tool framework."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    principal: AccessPrincipal


class ToolRefusedError(ToolError):
    """A call that was refused (not allowed, not available, switched off). Shown to the assistant.

    `code` is a short fixed word for the audit log; the message never contains secrets.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The caller of one tool call."""

    user_id: int
    client_id: str
    grant_id: int
    allowed_connection_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ResolvedConnection:
    """An allowed connection with the facts the framework checks before a call."""

    ref: ConnectionRef
    status: str
    writes_enabled: bool

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES


def current_principal() -> AccessPrincipal | None:
    """The principal of the request being served, or None outside an authenticated request."""
    token = get_access_token()
    return token.principal if isinstance(token, PrincipalToken) else None


async def build_context(principal: AccessPrincipal) -> ToolContext:
    """The context for a verified principal. A grant for all connections covers every connection
    that exists now (including ones added after the person approved)."""
    if principal.all_connections:
        async with session() as db:
            ids = frozenset((await db.scalars(select(PMSConnection.id))).all())
    else:
        ids = frozenset(principal.allowed_connection_ids)
    return ToolContext(
        user_id=principal.user_id,
        client_id=principal.client_id,
        grant_id=principal.grant_id,
        allowed_connection_ids=ids,
    )


async def allowed_connections(ctx: ToolContext) -> list[ResolvedConnection]:
    """The connections the token allows, by label. Ids that no longer exist are left out."""
    if not ctx.allowed_connection_ids:
        return []
    async with session() as db:
        rows = (
            await db.scalars(
                select(PMSConnection)
                .where(PMSConnection.id.in_(ctx.allowed_connection_ids))
                .order_by(PMSConnection.label)
            )
        ).all()
    return [
        ResolvedConnection(
            ref=ConnectionRef(
                connection_id=row.id, provider=Provider(row.provider), label=row.label
            ),
            status=row.status,
            writes_enabled=row.writes_enabled,
        )
        for row in rows
    ]


def _names(options: list[ResolvedConnection]) -> str:
    shown = [f'"{item.ref.label}"' for item in options[:MAX_NAMES_LISTED]]
    more = len(options) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _pick(options: list[ResolvedConnection], wanted: str) -> ResolvedConnection | None:
    for item in options:
        if item.ref.connection_id == wanted:
            return item
    for item in options:
        if item.ref.label == wanted:
            return item
    folded = [item for item in options if item.ref.label.casefold() == wanted.casefold()]
    return folded[0] if len(folded) == 1 else None


async def resolve_connection_state(ctx: ToolContext, connection: str | None) -> ResolvedConnection:
    """The allowed connection the call is about. Raises `ToolRefusedError` with a message to show.

    With no `connection`, the only allowed connection is used; with several, the caller must say
    which one.
    """
    options = await allowed_connections(ctx)
    wanted = (connection or "").strip()
    if not wanted:
        if len(options) == 1:
            return options[0]
        if not options:
            raise ToolRefusedError(
                "No connections are available to this AI assistant. The person who connected it "
                "can choose connections on the Connected apps page, and an administrator can add "
                "connections on the Connections page.",
                "no_connections",
            )
        raise ToolRefusedError(
            f"Several connections are available: {_names(options)}. "
            "Say which one with the connection argument.",
            "connection_required",
        )
    found = _pick(options, wanted)
    if found is None:
        # The same answer whether the connection does not exist or is not allowed.
        available = f" Available connections: {_names(options)}." if options else ""
        raise ToolRefusedError(
            f"There is no connection with that name or id.{available}", "unknown_connection"
        )
    return found


async def resolve_connection(ctx: ToolContext, connection: str | None) -> ConnectionRef:
    """`resolve_connection_state`, returning only the connection reference."""
    return (await resolve_connection_state(ctx, connection)).ref
