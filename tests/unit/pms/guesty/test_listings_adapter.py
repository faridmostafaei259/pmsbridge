"""The Guesty listing adapters through the real client, with respx as the only network."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
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
    PMSError,
    PMSNotFoundError,
    PMSPermissionError,
    PMSRateLimitedError,
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


# ---- photo changes ----

A1, A2, A3 = (f"6a1f0000000000000000a00{n}" for n in (1, 2, 3))
LIST_URL = f"{PHOTOS_URL}/{ID}"


def photo_list(router: respx.MockRouter, *answers: object) -> respx.Route:
    """The photo list read; each call gets the next answer (the last one repeats)."""
    queue = list(answers or (fixture("photos_three.json"),))

    def answer(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item if isinstance(item, httpx.Response) else json_response(item)

    return router.get(LIST_URL).mock(side_effect=answer)


def posted(route: respx.Route) -> object:
    return json.loads(route.calls.last.request.content)


async def test_a_caption_change_sends_the_caption_alone_once(router: respx.MockRouter) -> None:
    reads = photo_list(router)
    change = router.post(f"{LIST_URL}/{A2}").mock(
        return_value=json_response(fixture("photos_captioned.json"), 201)
    )
    photo = await content.build(make_client()).update_photo_caption(
        PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="Kitchen view")
    )
    assert posted(change) == {"caption": "Kitchen view"}  # never an address, which replaces it
    assert change.call_count == 1 and reads.call_count == 1
    assert photo.id == A2 and photo.caption == "Kitchen view" and photo.position == 1


async def test_an_empty_caption_is_sent_as_an_empty_caption(router: respx.MockRouter) -> None:
    photo_list(router)
    change = router.post(f"{LIST_URL}/{A2}").mock(
        return_value=json_response(fixture("photos_three.json"), 201)
    )
    await content.build(make_client()).update_photo_caption(
        PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="")
    )
    assert posted(change) == {"caption": ""}


async def test_a_caption_for_a_photo_that_is_not_listed_sends_nothing(
    router: respx.MockRouter,
) -> None:
    photo_list(router)
    change = router.post(url__regex=r".*").mock(return_value=json_response([], 201))
    with pytest.raises(PMSNotFoundError, match="nothing was changed"):
        await content.build(make_client()).update_photo_caption(
            PhotoCaptionUpdate(listing_id=ID, photo_id="other", caption="x")
        )
    assert change.call_count == 0


async def test_a_caption_answer_that_cannot_be_read_says_it_was_probably_changed(
    router: respx.MockRouter,
) -> None:
    photo_list(router)
    for body in ({"photos": []}, fixture("photos_reordered.json")[:1]):
        router.post(f"{LIST_URL}/{A2}").mock(return_value=json_response(body, 201))
        with pytest.raises(PMSUpstreamError, match="probably changed"):
            await content.build(make_client()).update_photo_caption(
                PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="x")
            )


async def test_a_reorder_sends_the_full_order_once_and_returns_the_new_order(
    router: respx.MockRouter,
) -> None:
    reads = photo_list(router)
    change = router.post(f"{LIST_URL}/order").mock(
        return_value=json_response(fixture("photos_reordered.json"), 201)
    )
    photos = await content.build(make_client()).reorder_photos(
        PhotoOrder(listing_id=ID, photo_ids=[A3, A1, A2])
    )
    assert posted(change) == {"order": [A3, A1, A2]} and change.call_count == 1
    assert reads.call_count == 1
    assert [p.id for p in photos] == [A3, A1, A2] and [p.position for p in photos] == [0, 1, 2]


async def test_a_reorder_with_an_unknown_id_sends_nothing(router: respx.MockRouter) -> None:
    photo_list(router)
    change = router.post(url__regex=r".*").mock(return_value=json_response([], 201))
    with pytest.raises(PMSNotFoundError, match="nothing was changed"):
        await content.build(make_client()).reorder_photos(
            PhotoOrder(listing_id=ID, photo_ids=[A3, A1, "unknown"])
        )
    assert change.call_count == 0


async def test_a_reorder_that_leaves_a_photo_out_sends_nothing(router: respx.MockRouter) -> None:
    photo_list(router)
    change = router.post(url__regex=r".*").mock(return_value=json_response([], 201))
    with pytest.raises(PMSValidationError, match="leaves out 1 of the listing's photos"):
        await content.build(make_client()).reorder_photos(
            PhotoOrder(listing_id=ID, photo_ids=[A3, A1])
        )
    assert change.call_count == 0


async def test_a_deletion_is_confirmed_by_reading_the_list_again(router: respx.MockRouter) -> None:
    reads = photo_list(router, fixture("photos_three.json"), fixture("photos_after_delete.json"))
    # The answer to a deletion is not documented, so it must not matter what it holds.
    removal = router.delete(f"{LIST_URL}/{A2}").mock(return_value=httpx.Response(204))
    result = await content.build(make_client()).delete_photo(
        PhotoDelete(listing_id=ID, photo_id=A2)
    )
    assert result is None
    assert removal.call_count == 1 and reads.call_count == 2
    assert not removal.calls.last.request.content


@pytest.mark.parametrize(
    "answer",
    [json_response({"message": "private text"}), httpx.Response(200, content=b"not json at all")],
)
async def test_a_deletion_answer_is_never_relied_on(
    router: respx.MockRouter, answer: httpx.Response
) -> None:
    photo_list(router, fixture("photos_three.json"), fixture("photos_after_delete.json"))
    router.delete(f"{LIST_URL}/{A2}").mock(return_value=answer)
    await content.build(make_client()).delete_photo(PhotoDelete(listing_id=ID, photo_id=A2))


async def test_a_photo_that_is_still_listed_after_a_deletion_is_an_error(
    router: respx.MockRouter,
) -> None:
    photo_list(router)  # the photo is listed before and after
    removal = router.delete(f"{LIST_URL}/{A2}").mock(return_value=httpx.Response(200))
    with pytest.raises(PMSUpstreamError, match="still in the list"):
        await content.build(make_client()).delete_photo(PhotoDelete(listing_id=ID, photo_id=A2))
    assert removal.call_count == 1  # and it is not sent again


async def test_a_failed_read_after_a_deletion_says_it_could_not_be_confirmed(
    router: respx.MockRouter,
) -> None:
    photo_list(router, fixture("photos_three.json"), json_response({"message": "x"}, 404))
    removal = router.delete(f"{LIST_URL}/{A2}").mock(return_value=httpx.Response(204))
    with pytest.raises(PMSUpstreamError, match="could not be read afterwards to confirm it"):
        await content.build(make_client()).delete_photo(PhotoDelete(listing_id=ID, photo_id=A2))
    assert removal.call_count == 1


async def test_deleting_a_photo_that_is_not_listed_sends_nothing(router: respx.MockRouter) -> None:
    photo_list(router)
    removal = router.delete(url__regex=r".*").mock(return_value=httpx.Response(204))
    with pytest.raises(PMSNotFoundError, match="nothing was changed"):
        await content.build(make_client()).delete_photo(
            PhotoDelete(listing_id=ID, photo_id="other")
        )
    assert removal.call_count == 0


async def test_a_listing_that_does_not_exist_is_not_found_before_any_change(
    router: respx.MockRouter,
) -> None:
    photo_list(router, json_response({"error": {"message": "private", "status": 404}}, 404))
    changes = router.route(method__in=["POST", "DELETE"]).mock(return_value=json_response([]))
    api = content.build(make_client())
    with pytest.raises(PMSNotFoundError):
        await api.update_photo_caption(PhotoCaptionUpdate(listing_id=ID, photo_id=A1, caption="x"))
    with pytest.raises(PMSNotFoundError):
        await api.delete_photo(PhotoDelete(listing_id=ID, photo_id=A1))
    assert changes.call_count == 0


@pytest.mark.parametrize("status", [400, 403, 422, 500])
async def test_a_refused_change_is_not_sent_again_and_hides_the_upstream_text(
    router: respx.MockRouter, status: int
) -> None:
    photo_list(router)
    answer = json_response({"message": "private upstream"}, status)
    caption = router.post(f"{LIST_URL}/{A2}").mock(return_value=answer)
    order = router.post(f"{LIST_URL}/order").mock(return_value=answer)
    removal = router.delete(f"{LIST_URL}/{A2}").mock(return_value=answer)
    api = content.build(make_client())
    attempts = (
        lambda: api.update_photo_caption(
            PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="x")
        ),
        lambda: api.reorder_photos(PhotoOrder(listing_id=ID, photo_ids=[A3, A1, A2])),
        lambda: api.delete_photo(PhotoDelete(listing_id=ID, photo_id=A2)),
    )
    for attempt in attempts:
        with pytest.raises(PMSError) as caught:
            await attempt()
        assert "private upstream" not in caught.value.message
    assert (caption.call_count, order.call_count, removal.call_count) == (1, 1, 1)


async def test_a_timeout_means_the_outcome_is_unknown_and_nothing_is_sent_again(
    router: respx.MockRouter,
) -> None:
    reads = photo_list(router)
    caption = router.post(f"{LIST_URL}/{A2}").mock(side_effect=httpx.ReadTimeout("slow"))
    order = router.post(f"{LIST_URL}/order").mock(side_effect=httpx.ReadTimeout("slow"))
    removal = router.delete(f"{LIST_URL}/{A2}").mock(side_effect=httpx.ReadTimeout("slow"))
    api = content.build(make_client())
    attempts = (
        lambda: api.update_photo_caption(
            PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="x")
        ),
        lambda: api.reorder_photos(PhotoOrder(listing_id=ID, photo_ids=[A3, A1, A2])),
        lambda: api.delete_photo(PhotoDelete(listing_id=ID, photo_id=A2)),
    )
    for attempt in attempts:
        with pytest.raises(PMSUpstreamError, match="outcome is unknown"):
            await attempt()
    assert (caption.call_count, order.call_count, removal.call_count) == (1, 1, 1)
    assert reads.call_count == 3  # one list read before each change, none after a timeout


async def test_a_429_with_a_wait_time_is_sent_again_once_and_one_without_is_not(
    router: respx.MockRouter,
) -> None:
    photo_list(router)
    caption = router.post(f"{LIST_URL}/{A2}").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            json_response(fixture("photos_captioned.json"), 201),
        ]
    )
    api = content.build(make_client())
    await api.update_photo_caption(PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="x"))
    assert caption.call_count == 2
    removal = router.delete(f"{LIST_URL}/{A2}").mock(return_value=httpx.Response(429))
    with pytest.raises(PMSRateLimitedError):
        await api.delete_photo(PhotoDelete(listing_id=ID, photo_id=A2))
    assert removal.call_count == 1


async def test_too_little_time_left_sends_no_change(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo_list(router)
    changes = router.route(method__in=["POST", "DELETE"]).mock(return_value=json_response([]))
    monkeypatch.setattr(content, "remaining_seconds", lambda: 0.5)
    api = content.build(make_client())
    for attempt in (
        lambda: api.update_photo_caption(
            PhotoCaptionUpdate(listing_id=ID, photo_id=A2, caption="x")
        ),
        lambda: api.reorder_photos(PhotoOrder(listing_id=ID, photo_ids=[A3, A1, A2])),
        lambda: api.delete_photo(PhotoDelete(listing_id=ID, photo_id=A2)),
    ):
        with pytest.raises(PMSUpstreamError, match="ran out"):
            await attempt()
    assert changes.call_count == 0


async def test_ids_stay_inside_their_path_segments(router: respx.MockRouter) -> None:
    router.get(url__regex=r".*").mock(return_value=json_response(fixture("photos_three.json")))
    removal = router.delete(url__regex=r".*").mock(return_value=httpx.Response(204))
    api = content.build(make_client())
    with pytest.raises(PMSNotFoundError):  # the photo is not listed, so nothing is deleted
        await api.delete_photo(PhotoDelete(listing_id="a/b", photo_id="c/d"))
    assert removal.call_count == 0
    assert router.calls.last.request.url.raw_path.decode().endswith("/property-photos/a%2Fb")
    router.post(url__regex=r".*").mock(return_value=json_response([], 201))
    photo_id_with_slash = {
        "_id": "c/d",
        "original": "https://images.example.test/d.jpg",
        "index": 0,
    }
    router.get(url__regex=r".*").mock(return_value=json_response([photo_id_with_slash]))
    with pytest.raises(PMSUpstreamError):
        await api.update_photo_caption(
            PhotoCaptionUpdate(listing_id="a/b", photo_id="c/d", caption="x")
        )
    assert router.calls.last.request.url.raw_path.decode().endswith("/a%2Fb/c%2Fd")


async def test_no_photo_change_logs_the_caption_or_the_upstream_text(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    photo_list(router, fixture("photos_three.json"), json_response({"message": "private"}, 500))
    router.delete(f"{LIST_URL}/{A2}").mock(return_value=httpx.Response(204))
    router.post(f"{LIST_URL}/{A1}").mock(
        return_value=json_response({"message": "secret caption words"}, 500)
    )
    api = content.build(make_client())
    with pytest.raises(PMSError):
        await api.delete_photo(PhotoDelete(listing_id=ID, photo_id=A2))
    with pytest.raises(PMSError):
        await api.update_photo_caption(
            PhotoCaptionUpdate(listing_id=ID, photo_id=A1, caption="secret caption words")
        )
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "secret caption words" not in recorded and "private" not in recorded


def test_capabilities_and_wiring() -> None:
    assert {Capability.LISTINGS_READ} == listings.CAPABILITIES
    assert {
        Capability.LISTING_DETAILS_READ,
        Capability.PHOTOS_READ,
        Capability.PHOTOS_WRITE,
    } == content.CAPABILITIES
    assert DOMAIN_MODULES["content"] == "content"
    found = discover_domains()
    assert found["listings"].__name__ == "pmsbridge.pms.guesty.listings"
    assert found["content"].__name__ == "pmsbridge.pms.guesty.content"
    assert found["listings"].CAPABILITIES == listings.CAPABILITIES
    assert found["content"].CAPABILITIES == content.CAPABILITIES
