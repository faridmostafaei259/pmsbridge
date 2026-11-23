"""Owner tools: list the property owners and look one up.

Both are read-only and work on any connection whose provider supports them. They return the shared
owner model only: name, email, phone and the listings the owner has. Payout and bank details are
never part of it.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from pmsbridge.mcp.framework import ToolCall, clamp_limit, pms_tool
from pmsbridge.pms.base import Capability, OwnersAPI, PMSValidationError
from pmsbridge.pms.models import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Owner, Page, PageRequest

MAX_CURSOR_LENGTH = 1000


def register(mcp: MCPServer) -> None:
    @pms_tool(
        mcp,
        name="list_owners",
        capability=Capability.OWNERS_READ,
        api=lambda p: p.owners,
        title="List owners",
    )
    async def list_owners(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[OwnersAPI],
        *,
        limit: Annotated[
            int | None,
            Field(description=f"How many owners to return (1 to {MAX_PAGE_SIZE})."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="The next_cursor of the previous result, to get the next page."),
        ] = None,
    ) -> Page[Owner]:
        """List the property owners in the property management system.

        Returns a page of owners with name, email, phone and the ids of their listings (use
        get_listing for a listing). Payout, bank and tax details are never shown. When
        next_cursor is set there are more; pass it back as cursor.
        """
        if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
            raise PMSValidationError("Invalid pagination cursor.")
        request = PageRequest(
            limit=clamp_limit(limit, default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE),
            cursor=cursor,
        )
        return await call.api.list_owners(request)

    @pms_tool(
        mcp,
        name="get_owner",
        capability=Capability.OWNERS_READ,
        api=lambda p: p.owners,
        title="Get an owner",
    )
    async def get_owner(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[OwnersAPI],
        *,
        owner_id: Annotated[str, Field(description="The owner id, for example from list_owners.")],
    ) -> Owner:
        """Get one property owner by id: name, email, phone and the ids of their listings.

        Payout, bank and tax details are never shown. Fails with a clear message if the owner
        does not exist.
        """
        return await call.api.get_owner(owner_id)
