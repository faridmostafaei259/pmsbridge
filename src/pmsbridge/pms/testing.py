"""In-memory provider for tests.

Type-checked with the rest of ``src/`` to prove the provider contract is implementable.
"""

from __future__ import annotations

from pmsbridge.pms.base import (
    CalendarAPI,
    Capability,
    ConnectionRef,
    GuestsAPI,
    InboxAPI,
    ListingsAPI,
    OwnersAPI,
    PaymentsAPI,
    PMSNotFoundError,
    PMSProvider,
    PMSValidationError,
    Provider,
    ReservationsAPI,
    ReviewsAPI,
    TasksAPI,
)
from pmsbridge.pms.models import Listing, ListingQuery, Page


class FakeListingsAPI:
    """Serves a fixed list of listings with integer-offset cursors."""

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def list_listings(self, query: ListingQuery) -> Page[Listing]:
        offset = 0
        if query.cursor is not None:
            if not query.cursor.isdigit():
                raise PMSValidationError("Invalid pagination cursor.")
            offset = int(query.cursor)
        items = [
            listing
            for listing in self._listings
            if query.search is None or query.search.lower() in listing.title.lower()
        ]
        page = items[offset : offset + query.limit]
        next_offset = offset + query.limit
        next_cursor = str(next_offset) if next_offset < len(items) else None
        return Page[Listing](items=page, next_cursor=next_cursor, total=len(items))

    async def get_listing(self, listing_id: str) -> Listing:
        for listing in self._listings:
            if listing.id == listing_id:
                return listing
        raise PMSNotFoundError("Listing not found.")


class FakeProvider:
    """A provider that supports listings only."""

    def __init__(self, listings: list[Listing], label: str = "test") -> None:
        self._connection = ConnectionRef(
            connection_id="conn-1", provider=Provider.GUESTY, label=label
        )
        self._listings = FakeListingsAPI(listings)

    @property
    def provider(self) -> Provider:
        return self._connection.provider

    @property
    def connection(self) -> ConnectionRef:
        return self._connection

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.LISTINGS_READ})

    @property
    def listings(self) -> ListingsAPI | None:
        return self._listings

    @property
    def reservations(self) -> ReservationsAPI | None:
        return None

    @property
    def calendar(self) -> CalendarAPI | None:
        return None

    @property
    def guests(self) -> GuestsAPI | None:
        return None

    @property
    def inbox(self) -> InboxAPI | None:
        return None

    @property
    def reviews(self) -> ReviewsAPI | None:
        return None

    @property
    def owners(self) -> OwnersAPI | None:
        return None

    @property
    def payments(self) -> PaymentsAPI | None:
        return None

    @property
    def tasks(self) -> TasksAPI | None:
        return None

    async def aclose(self) -> None:
        return None


def as_provider(p: FakeProvider) -> PMSProvider:
    """Return ``p`` typed as ``PMSProvider``; pyright verifies structural conformance here."""
    return p


__all__ = ["FakeListingsAPI", "FakeProvider", "as_provider"]
