"""Listing tools: list and look up properties, read their details and photos, change photos.

The four reads work on any connection whose provider supports them. Changing a caption, reordering
the photos and deleting a photo are changes: the framework only lets them run on connections where
an administrator allowed changes. They return the shared listing models only.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    MAX_PHOTOS,
    Listing,
    ListingDetails,
    ListingPhoto,
    ListingQuery,
    Page,
    PhotoCaptionUpdate,
    PhotoDelete,
    PhotoOrder,
)

MAX_CURSOR_LENGTH = 1000
MAX_CAPTION_LENGTH = 500
BAD_PHOTO_INPUT = "The listing id or the photo ids are not valid."


class ListingPhotos(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str = Field(description="The listing the photos belong to.")
    photos: list[ListingPhoto] = Field(description="The photos in display order.")


class PhotoDeleted(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: str = Field(description="The listing the photo belonged to.")
    photo_id: str = Field(description="The photo that was deleted.")
    deleted: bool = Field(
        description="True: the photo list was read again and the photo is no longer in it."
    )


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

    @pms_tool(
        mcp,
        name="update_photo_caption",
        capability=Capability.PHOTOS_WRITE,
        api=lambda p: p.content,
        title="Change a photo caption",
    )
    async def update_photo_caption(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingContentAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
        photo_id: Annotated[str, Field(description="The photo id from list_listing_photos.")],
        caption: Annotated[
            str,
            Field(
                description=(
                    f"The new caption, up to {MAX_CAPTION_LENGTH} characters. "
                    "An empty text removes the caption."
                )
            ),
        ],
    ) -> ListingPhoto:
        """Changes the caption of one photo. The image, its position and its room stay as they are.

        Show the person the exact caption and get their confirmation first. The photo id must be
        one from list_listing_photos for the same listing; otherwise nothing is changed. The change
        is sent once and not sent again if something goes wrong; if the answer says the outcome is
        unknown, read the photos with list_listing_photos before trying again. Returns the photo as
        it is afterwards.
        """
        try:
            update = PhotoCaptionUpdate(listing_id=listing_id, photo_id=photo_id, caption=caption)
        except ValidationError as exc:
            fields = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            raise PMSValidationError(
                f"The caption must be at most {MAX_CAPTION_LENGTH} characters."
                if "caption" in fields
                else BAD_PHOTO_INPUT
            ) from None
        return await call.api.update_photo_caption(update)

    @pms_tool(
        mcp,
        name="reorder_photos",
        capability=Capability.PHOTOS_WRITE,
        api=lambda p: p.content,
        title="Reorder listing photos",
    )
    async def reorder_photos(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingContentAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
        photo_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Every photo id of the listing exactly once, in the order they should "
                    "appear (the first is the cover photo). Take the ids from list_listing_photos."
                ),
                max_length=MAX_PHOTOS,
            ),
        ],
    ) -> ListingPhotos:
        """Puts the photos of one property in a new order.

        Give every photo id once, in the new display order. An id that is not one of the listing's
        photos, or a photo left out, is refused and nothing is changed. Show the person the new
        order and get their confirmation first. The change is sent once and not sent again if
        something goes wrong; if the answer says the outcome is unknown, read the photos with
        list_listing_photos before trying again. Returns the photos in their new order.
        """
        try:
            order = PhotoOrder(listing_id=listing_id, photo_ids=photo_ids)
        except ValidationError:
            raise PMSValidationError(
                "Give the listing id and every photo id once, with no repeats."
            ) from None
        photos = await call.api.reorder_photos(order)
        return ListingPhotos(listing_id=order.listing_id, photos=photos)

    @pms_tool(
        mcp,
        name="delete_photo",
        capability=Capability.PHOTOS_WRITE,
        api=lambda p: p.content,
        title="Delete a listing photo",
    )
    async def delete_photo(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[ListingContentAPI],
        *,
        listing_id: Annotated[str, Field(description="The listing id from list_listings.")],
        photo_id: Annotated[str, Field(description="The photo id from list_listing_photos.")],
    ) -> PhotoDeleted:
        """Deletes one photo from a property. This cannot be undone.

        Only call this after the person has explicitly confirmed, in this conversation, that this
        exact photo (show its caption and position) is to be deleted for good. The photo id must be
        one from list_listing_photos for the same listing; otherwise nothing is deleted. The
        deletion is sent once and not sent again if something goes wrong; the photo list is read
        again afterwards to confirm the photo is gone. If the answer says the outcome is unknown or
        could not be confirmed, read the photos with list_listing_photos before trying again.
        """
        try:
            photo = PhotoDelete(listing_id=listing_id, photo_id=photo_id)
        except ValidationError:
            raise PMSValidationError(BAD_PHOTO_INPUT) from None
        await call.api.delete_photo(photo)  # raises unless the photo is confirmed gone
        return PhotoDeleted(listing_id=photo.listing_id, photo_id=photo.photo_id, deleted=True)
