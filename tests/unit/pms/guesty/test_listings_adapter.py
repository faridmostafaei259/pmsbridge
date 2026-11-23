"""The Guesty listing adapters through the real client, with respx as the only network."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import respx
from guesty_listing_helpers import (
    LISTINGS_URL,
    PHOTOS_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms.base import (
    Capability,
    CapabilityNotSupportedError,
    PMSNotFoundError,
    PMSPermissionError,
    PMSUpstreamError,
    PMSValidationError,
)
from pmsbridge.pms.guesty import content, listings
from pmsbridge.pms.guesty.common import decode_cursor, encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import (
    ListingQuery,
    ListingStatus,
    PhotoCaptionUpdate,
    PhotoDelete,
    PhotoOrder,
)

ID = "6a1f00000000000000000001"


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


async def test_list_sends_the_documented_parameters(router: respx.MockRouter) -> None:
    route = router.get(LISTINGS_URL).mock(return_value=json_response([]))
    page = await listings.build(make_client()).list_listings(
        ListingQuery(limit=10, active_only=True, search="loft")
    )
    assert page.items == [] and page.next_cursor is None and page.total is None
    params = query_of(route)
    assert params["limit"] == "10" and params["skip"] == "0"
    assert params["active"] == "true" and params["q"] == "loft" and params["sort"] == "title"
    assert "integrations" not in params["fields"].split()
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_active_is_left_out_unless_asked_for(router: respx.MockRouter) -> None:
    route = router.get(LISTINGS_URL).mock(return_value=json_response([]))
    api = listings.build(make_client())
    for active_only in (None, False):
        await api.list_listings(ListingQuery(active_only=active_only))
        assert "active" not in query_of(route) and "q" not in query_of(route)


async def test_a_bare_array_response(router: respx.MockRouter) -> None:
    router.get(LISTINGS_URL).mock(return_value=json_response(fixture("listings_bare_array.json")))
    page = await listings.build(make_client()).list_listings(ListingQuery(limit=2))
    assert [item.title for item in page.items] == ["Alpine Chalet", "Beach Bungalow"]
    assert [item.status for item in page.items] == [ListingStatus.ACTIVE, ListingStatus.INACTIVE]
    assert page.total is None
    assert page.next_cursor is not None  # a full page: there may be more
    assert decode_cursor(page.next_cursor) == 2


async def test_a_short_page_has_no_next_cursor(router: respx.MockRouter) -> None:
    router.get(LISTINGS_URL).mock(return_value=json_response(fixture("listings_bare_array.json")))
    page = await listings.build(make_client()).list_listings(ListingQuery(limit=3))
    assert len(page.items) == 2 and page.next_cursor is None


async def test_an_envelope_response_with_a_total(router: respx.MockRouter) -> None:
    route = router.get(LISTINGS_URL).mock(
        return_value=json_response(fixture("listings_envelope.json"))
    )
    api = listings.build(make_client())
    first = await api.list_listings(ListingQuery(limit=2))
    assert [item.title for item in first.items] == ["City Studio", "Forest Cabin"]
    assert first.total == 5 and first.next_cursor is not None
    second = await api.list_listings(ListingQuery(limit=2, cursor=first.next_cursor))
    assert query_of(route)["skip"] == "2"
    assert decode_cursor(second.next_cursor) == 4
    last = await api.list_listings(ListingQuery(limit=2, cursor=second.next_cursor))
    assert query_of(route)["skip"] == "4"
    assert last.next_cursor is None  # skip 4 + 2 returned reaches the total of 5


async def test_the_last_page_of_an_envelope_ends(router: respx.MockRouter) -> None:
    body: dict[str, Any] = {"results": [{"_id": "a", "title": "A"}], "total": 3}
    router.get(LISTINGS_URL).mock(return_value=json_response(body))
    cursor = encode_cursor(2)
    page = await listings.build(make_client()).list_listings(ListingQuery(limit=5, cursor=cursor))
    assert page.next_cursor is None and page.total == 3


async def test_malformed_items_are_skipped_but_still_counted(router: respx.MockRouter) -> None:
    router.get(LISTINGS_URL).mock(
        return_value=json_response(fixture("listings_with_malformed.json"))
    )
    page = await listings.build(make_client()).list_listings(ListingQuery(limit=6))
    assert [item.id for item in page.items] == [
        "6a1f00000000000000000031",
        "6a1f00000000000000000032",
    ]
    assert decode_cursor(page.next_cursor) == 6


async def test_more_items_than_asked_are_cut_to_the_limit(router: respx.MockRouter) -> None:
    router.get(LISTINGS_URL).mock(return_value=json_response(fixture("listings_bare_array.json")))
    page = await listings.build(make_client()).list_listings(ListingQuery(limit=1))
    assert len(page.items) == 1 and decode_cursor(page.next_cursor) == 1


@pytest.mark.parametrize("body", ["text", 7, None, {"nothing": []}, {"results": "no"}])
async def test_an_unreadable_list_is_an_upstream_error(
    router: respx.MockRouter, body: object
) -> None:
    router.get(LISTINGS_URL).mock(return_value=json_response(body))
    with pytest.raises(PMSUpstreamError):
        await listings.build(make_client()).list_listings(ListingQuery())


async def test_a_malformed_cursor_is_refused_before_any_request(router: respx.MockRouter) -> None:
    route = router.get(LISTINGS_URL).mock(return_value=json_response([]))
    with pytest.raises(PMSValidationError):
        await listings.build(make_client()).list_listings(ListingQuery(cursor="https://evil.test"))
    assert route.call_count == 0


async def test_get_listing(router: respx.MockRouter) -> None:
    route = router.get(f"{LISTINGS_URL}/{ID}").mock(
        return_value=json_response(fixture("listing_full.json"))
    )
    listing = await listings.build(make_client()).get_listing(ID)
    assert listing.id == ID and listing.title == "Harbour View Loft"
    assert "integrations" not in query_of(route)["fields"].split()


async def test_a_missing_listing_is_not_found(router: respx.MockRouter) -> None:
    router.get(f"{LISTINGS_URL}/{ID}").mock(
        return_value=json_response({"error": {"message": "secret upstream words"}}, 404)
    )
    with pytest.raises(PMSNotFoundError) as caught:
        await listings.build(make_client()).get_listing(ID)
    assert "secret upstream words" not in caught.value.message


async def test_an_unusable_listing_response_is_an_upstream_error(router: respx.MockRouter) -> None:
    router.get(f"{LISTINGS_URL}/{ID}").mock(return_value=json_response({"title": "no id"}))
    with pytest.raises(PMSUpstreamError):
        await listings.build(make_client()).get_listing(ID)


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [
        ("a/b", "a%2Fb"),
        ("../admin", "..%2Fadmin"),
        ("a?x=1", "a%3Fx%3D1"),
        ("a#frag", "a%23frag"),
        ("a b", "a%20b"),
        ("%2e%2e", "%252e%252e"),
    ],
)
async def test_an_id_never_changes_the_path(
    router: respx.MockRouter, hostile: str, expected: str
) -> None:
    route = router.get(url__regex=r".*").mock(return_value=json_response({"_id": "x"}))
    api = listings.build(make_client())
    try:
        await api.get_listing(hostile)
    except PMSValidationError:
        # The client refuses paths that still hold a dot segment once decoded.
        assert ".." in hostile or "%2e" in hostile.lower()
        assert route.call_count == 0
        return
    assert route.calls.last.request.url.raw_path.decode().split("?")[0] == (
        f"/v1/listings/{expected}"
    )


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201])
async def test_an_empty_or_huge_id_is_refused(router: respx.MockRouter, bad: str) -> None:
    route = router.get(url__regex=r".*").mock(return_value=json_response({}))
    with pytest.raises(PMSValidationError):
        await listings.build(make_client()).get_listing(bad)
    with pytest.raises(PMSValidationError):
        await content.build(make_client()).list_listing_photos(bad)
    with pytest.raises(PMSValidationError):
        await content.build(make_client()).get_listing_details(bad)
    assert route.call_count == 0


async def test_details(router: respx.MockRouter) -> None:
    route = router.get(f"{LISTINGS_URL}/{ID}").mock(
        return_value=json_response(fixture("details_full.json"))
    )
    details = await content.build(make_client()).get_listing_details(f" {ID} ")
    assert details.listing_id == ID
    assert details.amenities == ["Wifi", "Kitchen", "Heating"]
    requested = query_of(route)["fields"].split()
    assert "integrations" not in requested
    assert "privateDescription" not in requested


async def test_details_of_a_missing_listing(router: respx.MockRouter) -> None:
    router.get(f"{LISTINGS_URL}/{ID}").mock(return_value=json_response({}, 404))
    with pytest.raises(PMSNotFoundError):
        await content.build(make_client()).get_listing_details(ID)


async def test_details_with_an_unreadable_body(router: respx.MockRouter) -> None:
    router.get(f"{LISTINGS_URL}/{ID}").mock(return_value=json_response(["x"]))
    with pytest.raises(PMSUpstreamError):
        await content.build(make_client()).get_listing_details(ID)


async def test_photos_are_sorted_by_index_and_unusable_ones_dropped(
    router: respx.MockRouter,
) -> None:
    route = router.get(f"{PHOTOS_URL}/{ID}").mock(
        return_value=json_response(fixture("photos.json"))
    )
    photos = await content.build(make_client()).list_listing_photos(ID)
    assert [photo.id for photo in photos] == [
        "6a1f0000000000000000a001",
        "6a1f0000000000000000a002",
        "6a1f0000000000000000a003",
    ]
    assert [photo.position for photo in photos] == [0, 1, 2]
    assert photos[0].caption == "First" and photos[1].thumbnail_url is None
    assert route.call_count == 1


async def test_photo_errors(router: respx.MockRouter) -> None:
    api = content.build(make_client())
    router.get(f"{PHOTOS_URL}/missing").mock(return_value=json_response({}, 404))
    with pytest.raises(PMSNotFoundError):
        await api.list_listing_photos("missing")
    router.get(f"{PHOTOS_URL}/denied").mock(return_value=json_response({}, 403))
    with pytest.raises(PMSPermissionError):
        await api.list_listing_photos("denied")
    router.get(f"{PHOTOS_URL}/odd").mock(return_value=json_response({"photos": []}))
    with pytest.raises(PMSUpstreamError):
        await api.list_listing_photos("odd")


async def test_photo_ids_are_quoted_in_the_path(router: respx.MockRouter) -> None:
    route = router.get(url__regex=r".*").mock(return_value=json_response([]))
    await content.build(make_client()).list_listing_photos("a/b")
    assert route.calls.last.request.url.raw_path.decode() == (
        "/v1/properties-api/property-photos/property-photos/a%2Fb"
    )


async def test_photo_writes_are_refused_and_send_nothing(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    api = content.build(make_client())
    with pytest.raises(CapabilityNotSupportedError):
        await api.update_photo_caption(
            PhotoCaptionUpdate(listing_id=ID, photo_id="p1", caption="New")
        )
    with pytest.raises(CapabilityNotSupportedError):
        await api.reorder_photos(PhotoOrder(listing_id=ID, photo_ids=["p1", "p2"]))
    with pytest.raises(CapabilityNotSupportedError):
        await api.delete_photo(PhotoDelete(listing_id=ID, photo_id="p1"))
    assert route.call_count == 0


def test_capabilities_and_wiring() -> None:
    assert {Capability.LISTINGS_READ} == listings.CAPABILITIES
    assert {Capability.LISTING_DETAILS_READ, Capability.PHOTOS_READ} == content.CAPABILITIES
    assert Capability.PHOTOS_WRITE not in content.CAPABILITIES
    assert DOMAIN_MODULES["content"] == "content"
    found = discover_domains()
    assert found["listings"].__name__ == "pmsbridge.pms.guesty.listings"
    assert found["content"].__name__ == "pmsbridge.pms.guesty.content"
    assert found["listings"].CAPABILITIES == listings.CAPABILITIES
    assert found["content"].CAPABILITIES == content.CAPABILITIES
