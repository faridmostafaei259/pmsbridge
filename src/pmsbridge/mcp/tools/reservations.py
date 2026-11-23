"""Reservation tools: list bookings with filters and read one booking in detail.

Both are read-only and work on any connection whose provider supports them. They return the shared
reservation models only.
"""

from datetime import date
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from pmsbridge.mcp.framework import ToolCall, clamp_limit, pms_tool
from pmsbridge.pms.base import Capability, PMSValidationError, ReservationsAPI
from pmsbridge.pms.models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    Reservation,
    ReservationQuery,
    ReservationStatus,
)

MAX_CURSOR_LENGTH = 1000
MAX_STATUSES = 10
MAX_TEXT_FILTER = 200
DATE_HELP = "a date written YYYY-MM-DD"


def _check_range(start: date | None, end: date | None, name: str) -> None:
    if start and end and end < start:
        raise PMSValidationError(f"{name}_to must not be before {name}_from.")


def register(mcp: MCPServer) -> None:
    @pms_tool(
        mcp,
        name="list_reservations",
        capability=Capability.RESERVATIONS_READ,
        api=lambda p: p.reservations,
        title="List reservations",
    )
    async def list_reservations(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ReservationsAPI],
        *,
        listing_id: Annotated[
            str | None,
            Field(
                description="Only reservations for this listing id (from list_listings).",
                max_length=MAX_TEXT_FILTER,
            ),
        ] = None,
        statuses: Annotated[
            list[ReservationStatus] | None,
            Field(
                description=(
                    "Only reservations in one of these statuses: inquiry, pending, confirmed, "
                    "checked_in, checked_out, cancelled, declined, expired or unknown. "
                    "Omit for every status."
                ),
                max_length=MAX_STATUSES,
            ),
        ] = None,
        check_in_from: Annotated[
            date | None, Field(description=f"Check-in on or after this day, {DATE_HELP}.")
        ] = None,
        check_in_to: Annotated[
            date | None, Field(description=f"Check-in on or before this day, {DATE_HELP}.")
        ] = None,
        check_out_from: Annotated[
            date | None, Field(description=f"Check-out on or after this day, {DATE_HELP}.")
        ] = None,
        check_out_to: Annotated[
            date | None, Field(description=f"Check-out on or before this day, {DATE_HELP}.")
        ] = None,
        created_from: Annotated[
            date | None, Field(description=f"Booked on or after this day, {DATE_HELP}.")
        ] = None,
        created_to: Annotated[
            date | None, Field(description=f"Booked on or before this day, {DATE_HELP}.")
        ] = None,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "A confirmation code or guest name to look for. What is matched depends on "
                    "the property management system; if a name finds nothing, list by date "
                    "instead and check the guest on each booking with get_reservation. "
                    "Omit for all."
                ),
                max_length=MAX_TEXT_FILTER,
            ),
        ] = None,
        limit: Annotated[
            int | None,
            Field(description=f"How many reservations to return (1 to {MAX_PAGE_SIZE})."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="The next_cursor of the previous result, to get the next page."),
        ] = None,
    ) -> Page[Reservation]:
        """List reservations, earliest check-in first, optionally filtered.

        The earliest check-in comes first (set check_in_from to leave out past bookings). Returns a
        page of reservations with dates, nights, status, channel, confirmation code and guest
        count. List results leave out the guest's contact details and the money; use
        get_reservation for one booking to get them when the property management system provides
        them. Dates are written YYYY-MM-DD. When next_cursor is set there are more; pass it back
        as cursor.
        """
        if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
            raise PMSValidationError("Invalid pagination cursor.")
        _check_range(check_in_from, check_in_to, "check_in")
        _check_range(check_out_from, check_out_to, "check_out")
        _check_range(created_from, created_to, "created")
        query = ReservationQuery(
            limit=clamp_limit(limit, default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE),
            cursor=cursor,
            listing_id=(listing_id or "").strip() or None,
            statuses=statuses or None,
            search=(search or "").strip() or None,
            check_in_from=check_in_from,
            check_in_to=check_in_to,
            check_out_from=check_out_from,
            check_out_to=check_out_to,
            created_from=created_from,
            created_to=created_to,
        )
        return await call.api.list_reservations(query)

    @pms_tool(
        mcp,
        name="get_reservation",
        capability=Capability.RESERVATIONS_READ,
        api=lambda p: p.reservations,
        title="Get a reservation",
    )
    async def get_reservation(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ReservationsAPI],
        *,
        reservation_id: Annotated[
            str, Field(description="The reservation id from list_reservations.")
        ],
    ) -> Reservation:
        """Get one reservation by its id, with the guest's contact details and balance.

        Returns the dates, status, channel, confirmation code, notes, and, when the property
        management system provides them, the guest's name, email and phone and the balance the
        guest still owes. Door and lock codes are never shown. Fails with a clear message if the
        reservation does not exist.
        """
        return await call.api.get_reservation(reservation_id)
