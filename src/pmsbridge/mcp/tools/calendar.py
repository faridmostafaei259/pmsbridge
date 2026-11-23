"""Calendar and pricing tools: read availability and prices, read price settings, change them.

`get_calendar` and `get_pricing_settings` are read-only. `update_calendar` changes data: the
framework only runs it on connections where an administrator allowed changes, limits how often an
approval may use it, and never repeats it. All three return the shared calendar and pricing models.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pmsbridge.mcp.framework import ToolCall, pms_tool
from pmsbridge.pms.base import CalendarAPI, Capability, PMSValidationError, PricingAPI
from pmsbridge.pms.models import (
    MAX_CALENDAR_RANGE_DAYS,
    MAX_CALENDAR_UPDATES,
    CalendarDay,
    CalendarRange,
    CalendarUpdate,
    CalendarUpdateRequest,
    Money,
    PricingSettings,
)

DATE_HELP = "a date written YYYY-MM-DD"
CURRENCY_LENGTH = 3
MAX_ID_LENGTH = 200


class CalendarDays(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str = Field(description="The listing the days belong to.")
    days: list[CalendarDay] = Field(description="One entry per day, in date order.")


class CalendarChanged(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str = Field(description="The listing that was changed.")
    updates_applied: int = Field(description="How many of the requested ranges were applied.")
    days: list[CalendarDay] = Field(description="The days in the changed ranges, as they are now.")


class CalendarChange(BaseModel):
    """One date range to change. Set at least one of price, min_nights and available."""

    model_config = ConfigDict(extra="forbid")

    start: Annotated[date, Field(description=f"First day, {DATE_HELP}.")]
    end: Annotated[date, Field(description=f"Last day (included), {DATE_HELP}.")]
    price: Annotated[
        Decimal | None,
        Field(description="The price per night, above zero, at most two decimals."),
    ] = None
    currency: Annotated[
        str | None,
        Field(
            description=(
                "The currency of price, such as EUR. Required with price, and it must be the "
                "listing's own currency (see get_pricing_settings)."
            ),
            max_length=CURRENCY_LENGTH,
        ),
    ] = None
    min_nights: Annotated[
        int | None, Field(description="The minimum stay in nights (1 to 365).")
    ] = None
    available: Annotated[
        bool | None,
        Field(description="true to open the days for booking, false to block them."),
    ] = None


def _listing(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_ID_LENGTH:
        raise PMSValidationError("The listing id is not valid.")
    return cleaned


def _update_of(change: CalendarChange, number: int) -> CalendarUpdate:
    """The canonical update, or a plain message about what is wrong with this range."""
    price: Money | None = None
    if change.price is not None:
        code = (change.currency or "").strip().upper()
        if len(code) != CURRENCY_LENGTH or not code.isalpha():
            raise PMSValidationError(
                f"Range {number}: currency is required with price, for example EUR."
            )
        price = Money(amount=change.price, currency=code)
    elif change.currency is not None:
        raise PMSValidationError(f"Range {number}: currency is only used together with price.")
    try:
        return CalendarUpdate(
            start=change.start,
            end=change.end,
            price=price,
            min_nights=change.min_nights,
            available=change.available,
        )
    except ValidationError as exc:
        reasons = "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        raise PMSValidationError(f"Range {number}: {reasons}.") from None


def register(mcp: MCPServer) -> None:
    @pms_tool(
        mcp,
        name="get_calendar",
        capability=Capability.CALENDAR_READ,
        api=lambda p: p.calendar,
        title="Get the calendar",
    )
    async def get_calendar(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[CalendarAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
        start: Annotated[date, Field(description=f"First day, {DATE_HELP}.")],
        end: Annotated[date, Field(description=f"Last day (included), {DATE_HELP}.")],
    ) -> CalendarDays:
        """Get the calendar of one property day by day: availability, price and minimum stay.

        Returns one entry per day from start to end, both included, at most 366 days. Each day says
        whether it is available, its price per night and minimum stay, and, when it is booked, the
        reservation id (use get_reservation for the guest). A blocked day has a short reason.
        Days the property management system does not return are left out.
        """
        if end < start:
            raise PMSValidationError("end must not be before start.")
        if (end - start).days >= MAX_CALENDAR_RANGE_DAYS:
            raise PMSValidationError(f"Ask for at most {MAX_CALENDAR_RANGE_DAYS} days at a time.")
        wanted = CalendarRange(listing_id=_listing(listing_id), start=start, end=end)
        return CalendarDays(listing_id=wanted.listing_id, days=await call.api.get_calendar(wanted))

    @pms_tool(
        mcp,
        name="get_pricing_settings",
        capability=Capability.PRICING_READ,
        api=lambda p: p.pricing,
        title="Get pricing settings",
    )
    async def get_pricing_settings(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[PricingAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
    ) -> PricingSettings:
        """Get the standing price settings of one property.

        Returns its currency, base and weekend price, cleaning fee, extra guest fee and how many
        guests are included, security deposit, weekly and monthly discounts (in percent) and
        minimum and maximum stay. Prices for single days are in get_calendar.
        """
        return await call.api.get_pricing_settings(_listing(listing_id))

    @pms_tool(
        mcp,
        name="update_calendar",
        capability=Capability.CALENDAR_WRITE,
        api=lambda p: p.calendar,
        title="Update the calendar",
    )
    async def update_calendar(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[CalendarAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
        updates: Annotated[
            list[CalendarChange],
            Field(
                description=(
                    "The ranges to change, applied in order. Each has start and end (YYYY-MM-DD) "
                    "and at least one of price (with currency), min_nights and available."
                ),
                min_length=1,
                max_length=MAX_CALENDAR_UPDATES,
            ),
        ],
    ) -> CalendarChanged:
        """Change prices, minimum stays or availability for date ranges of one property.

        Ask the person to confirm the exact dates and amounts before calling this. Each range sets
        a price per night, a minimum stay or availability (or several) for every day from start to
        end, both included. At most 31 ranges per call; prices are in the listing's own currency.
        A price that looks like a mistake, a different currency, an availability change on a
        booked day, or a change that cannot be checked against the current calendar is refused and
        nothing is sent. If a later range fails, the message says which ranges were applied.
        Returns the changed days as they are afterwards.
        """
        changes = [_update_of(change, number) for number, change in enumerate(updates, start=1)]
        request = CalendarUpdateRequest(listing_id=_listing(listing_id), updates=changes)
        days = await call.api.update_calendar(request)
        return CalendarChanged(
            listing_id=request.listing_id, updates_applied=len(changes), days=days
        )
