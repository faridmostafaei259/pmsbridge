"""Guest tools: list guests and look up one guest's contact details and language.

Both are read-only and work on any connection whose provider supports them. They return the shared
guest model only.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from pmsbridge.mcp.framework import ToolCall, clamp_limit, pms_tool
from pmsbridge.pms.base import Capability, GuestsAPI, PMSValidationError
from pmsbridge.pms.models import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Guest, GuestQuery, Page

MAX_CURSOR_LENGTH = 1000
MAX_TEXT_FILTER = 200


def register(mcp: MCPServer) -> None:
    @pms_tool(
        mcp,
        name="list_guests",
        capability=Capability.GUESTS_READ,
        api=lambda p: p.guests,
        title="List guests",
    )
    async def list_guests(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[GuestsAPI],
        *,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "A guest name or email to look for. Not every property management system "
                    "can search guests; if it cannot, the tool says so. Omit for all."
                ),
                max_length=MAX_TEXT_FILTER,
            ),
        ] = None,
        limit: Annotated[
            int | None,
            Field(description=f"How many guests to return (1 to {MAX_PAGE_SIZE})."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="The next_cursor of the previous result, to get the next page."),
        ] = None,
    ) -> Page[Guest]:
        """List the guests in the property management system.

        Returns a page of guests with name, email, phone and language. Searching guests by name or
        email depends on the system; the reliable way to reach the guest of a booking is
        get_reservation. When next_cursor is set there are more; pass it back as cursor.
        """
        if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
            raise PMSValidationError("Invalid pagination cursor.")
        query = GuestQuery(
            limit=clamp_limit(limit, default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE),
            cursor=cursor,
            search=(search or "").strip() or None,
        )
        return await call.api.list_guests(query)

    @pms_tool(
        mcp,
        name="get_guest",
        capability=Capability.GUESTS_READ,
        api=lambda p: p.guests,
        title="Get a guest",
    )
    async def get_guest(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[GuestsAPI],
        *,
        guest_id: Annotated[
            str, Field(description="The guest id, for example from list_guests or a reservation.")
        ],
    ) -> Guest:
        """Get one guest by id: name, email, phone and preferred language.

        Identity documents, birthdays, addresses and staff notes are never shown. Fails with a
        clear message if the guest does not exist.
        """
        return await call.api.get_guest(guest_id)
