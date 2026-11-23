"""Listing tools: list and look up properties, read their full details and their photos.

All four are read-only and work on any connection whose provider supports them. They return the
shared listing models only.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from pmsbridge.mcp.framework import ToolCall, clamp_limit, pms_tool
from pmsbridge.pms.base import (
    Capability,
    ListingContentAPI,
    ListingsAPI,
    PMSValidationError,
)
from pmsbridge.pms.models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Listing,
    ListingDetails,
    ListingPhoto,
    ListingQuery,
    Page,
)

MAX_CURSOR_LENGTH = 1000


class ListingPhotos(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str = Field(description="The listing the photos belong to.")
    photos: list[ListingPhoto] = Field(description="The photos in display order.")


def register(mcp: MCPServer) -> None:
    @pms_tool(
        mcp,
        name="list_listings",
        capability=Capability.LISTINGS_READ,
        api=lambda p: p.listings,
        title="List listings",
    )
    async def list_listings(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingsAPI],
        *,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "Text to look for in the listing title, address or internal note. Omit for all."
                )
            ),
        ] = None,
        active_only: Annotated[
            bool,
            Field(
                description=(
                    "Only active listings. Use false to also ask for inactive ones "
                    "(the property management system may still hide some)."
                )
            ),
        ] = True,
        limit: Annotated[
            int | None,
            Field(description=f"How many listings to return (1 to {MAX_PAGE_SIZE})."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="The next_cursor of the previous result, to get the next page."),
        ] = None,
    ) -> Page[Listing]:
        """List the properties (listings) in the property management system.

        Returns a page of listings with name, address, bedrooms, guest capacity, base price and
        check-in times. When next_cursor is set there are more; pass it back as cursor. Use
        get_listing for one property or get_listing_details for its descriptions and amenities.
        """
        if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
            raise PMSValidationError("Invalid pagination cursor.")
        query = ListingQuery(
            limit=clamp_limit(limit, default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE),
            cursor=cursor,
            active_only=active_only,
            search=(search or "").strip() or None,
        )
        return await call.api.list_listings(query)

    @pms_tool(
        mcp,
        name="get_listing",
        capability=Capability.LISTINGS_READ,
        api=lambda p: p.listings,
        title="Get a listing",
    )
    async def get_listing(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingsAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
    ) -> Listing:
        """Get one property (listing) by its id.

        Returns its name, address, bedrooms, guest capacity, base price, time zone and check-in
        and check-out times. Fails with a clear message if the listing does not exist.
        """
        return await call.api.get_listing(listing_id)

    @pms_tool(
        mcp,
        name="get_listing_details",
        capability=Capability.LISTING_DETAILS_READ,
        api=lambda p: p.content,
        title="Get listing details",
    )
    async def get_listing_details(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingContentAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
    ) -> ListingDetails:
        """Get the full content of one property: descriptions, amenities and check-in instructions.

        Returns the listing's descriptions, the amenities it has and does not have, how guests
        check in, and its custom fields, where the property management system has them.
        """
        return await call.api.get_listing_details(listing_id)

    @pms_tool(
        mcp,
        name="list_listing_photos",
        capability=Capability.PHOTOS_READ,
        api=lambda p: p.content,
        title="List listing photos",
    )
    async def list_listing_photos(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingContentAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
    ) -> ListingPhotos:
        """List the photos of one property in display order.

        Each photo has an image address, a thumbnail address, a caption and its position. At most
        200 photos are returned.
        """
        photos = await call.api.list_listing_photos(listing_id)
        return ListingPhotos(listing_id=listing_id.strip(), photos=photos)
