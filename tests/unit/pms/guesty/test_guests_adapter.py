"""The Guesty guest adapter through the real client, with respx as the only network."""

import logging
from collections.abc import AsyncIterator

import pytest
import respx
from guesty_guest_helpers import (
    GUEST_EMAIL,
    GUEST_ID,
    GUEST_NAME,
    GUEST_PHONE,
    GUESTS_URL,
    NEVER_REQUESTED,
    NEVER_SHOWN,
    OTHER_EMAIL,
    OTHER_NAME,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms.base import (
    Capability,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
)
from pmsbridge.pms.guesty import guests
from pmsbridge.pms.guesty.common import MAX_SKIP, decode_cursor, encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import GuestQuery


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


def api() -> guests.GuestyGuests:
    return guests.build(make_client())


# ---- get ----


async def test_get_maps_a_full_guest(router: respx.MockRouter) -> None:
    route = router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(
        return_value=json_response(fixture("guest.json"))
    )
    guest = await api().get_guest(GUEST_ID)
    assert guest.id == GUEST_ID and guest.full_name == GUEST_NAME
    assert guest.first_name == "Alex" and guest.last_name == "Invented"
    assert guest.email == GUEST_EMAIL and guest.phone == GUEST_PHONE
    assert guest.language == "fr" and guest.extras == {"returning_guest": True}
    shown = guest.model_dump_json()
    for private in NEVER_SHOWN:
        assert private not in shown
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_get_asks_only_for_the_mapped_fields(router: respx.MockRouter) -> None:
    route = router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(
        return_value=json_response(fixture("guest.json"))
    )
    await api().get_guest(GUEST_ID)
    fields = query_of(route)["fields"].split(" ")
    assert fields == [
        "_id",
        "firstName",
        "lastName",
        "fullName",
        "email",
        "phone",
        "preferredLanguage",
        "returningGuest",
    ]
    for name in NEVER_REQUESTED:
        assert name not in fields


async def test_get_maps_a_minimal_guest(router: respx.MockRouter) -> None:
    router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(return_value=json_response({"_id": GUEST_ID}))
    guest = await api().get_guest(GUEST_ID)
    assert guest.full_name == "Unknown" and guest.email is None and guest.phone is None
    assert guest.language is None and guest.extras == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("de", "de"),
        (["es", "en"], "es"),
        ([None, 5, " ", "it"], "it"),
        ([], None),
        (7, None),
        ({"a": "b"}, None),
    ],
)
async def test_language_is_a_string_or_the_first_string_of_a_list(
    router: respx.MockRouter, value: object, expected: str | None
) -> None:
    router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(
        return_value=json_response({"_id": GUEST_ID, "preferredLanguage": value})
    )
    assert (await api().get_guest(GUEST_ID)).language == expected


async def test_get_quotes_the_id_as_one_path_segment(router: respx.MockRouter) -> None:
    route = router.get(url__startswith=f"{GUESTS_URL}/").mock(
        return_value=json_response({"_id": "x"})
    )
    await api().get_guest("a/b?c d")
    assert route.calls.last.request.url.raw_path.startswith(b"/v1/guests-crud/a%2Fb%3Fc%20d")


async def test_get_refuses_an_empty_or_over_long_id_before_any_request(
    router: respx.MockRouter,
) -> None:
    route = router.route().mock(return_value=json_response({}))
    for bad in ("", "  ", "x" * 201):
        with pytest.raises(PMSValidationError):
            await api().get_guest(bad)
    assert route.call_count == 0


async def test_a_404_is_not_found(router: respx.MockRouter) -> None:
    router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(
        return_value=json_response({"message": "private upstream text"}, 404)
    )
    with pytest.raises(PMSNotFoundError) as caught:
        await api().get_guest(GUEST_ID)
    assert "private upstream text" not in str(caught.value)


async def test_a_body_without_an_id_is_an_upstream_error(router: respx.MockRouter) -> None:
    router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(return_value=json_response({"fullName": "X"}))
    with pytest.raises(PMSUpstreamError):
        await api().get_guest(GUEST_ID)
    router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(return_value=json_response(["not", "a", "dict"]))
    with pytest.raises(PMSUpstreamError):
        await api().get_guest(GUEST_ID)


# ---- list ----


async def test_list_sends_the_default_parameters(router: respx.MockRouter) -> None:
    route = router.get(GUESTS_URL).mock(return_value=json_response({"results": []}))
    page = await api().list_guests(GuestQuery(limit=10))
    assert page.items == [] and page.next_cursor is None and page.total is None
    params = query_of(route)
    assert params["limit"] == "10" and params["skip"] == "0"
    assert params["columns"] == guests.GUEST_FIELDS
    for name in NEVER_REQUESTED:
        assert name not in params["columns"].split(" ")
    assert set(params) == {"columns", "limit", "skip"}


async def test_list_maps_items_aliases_and_the_total(router: respx.MockRouter) -> None:
    router.get(GUESTS_URL).mock(return_value=json_response(fixture("list_page.json")))
    page = await api().list_guests(GuestQuery(limit=3))
    assert [g.id for g in page.items] == [
        "6a3000000000000000000a01",
        "6a3000000000000000000a02",
        "6a3000000000000000000a03",
    ]
    first, second, third = page.items
    assert first.language == "fr" and first.email == GUEST_EMAIL
    assert second.full_name == OTHER_NAME and second.email == OTHER_EMAIL
    assert second.phone == "+15550200" and second.extras == {"returning_guest": False}
    assert third.full_name == "Unknown"
    assert page.total == 7
    shown = page.model_dump_json()
    for private in NEVER_SHOWN:
        assert private not in shown


async def test_list_cursor_continues_by_skip(router: respx.MockRouter) -> None:
    route = router.get(GUESTS_URL).mock(return_value=json_response(fixture("list_page.json")))
    page = await api().list_guests(GuestQuery(limit=3))
    assert page.next_cursor == encode_cursor(3)
    await api().list_guests(GuestQuery(limit=3, cursor=page.next_cursor))
    assert query_of(route)["skip"] == "3"


async def test_list_has_no_cursor_on_the_last_page(router: respx.MockRouter) -> None:
    body = {"results": [{"_id": "g1"}, {"_id": "g2"}], "total": 2}
    router.get(GUESTS_URL).mock(return_value=json_response(body))
    page = await api().list_guests(GuestQuery(limit=2))
    assert page.next_cursor is None and page.total == 2


async def test_without_a_total_a_full_page_means_more(router: respx.MockRouter) -> None:
    router.get(GUESTS_URL).mock(return_value=json_response([{"_id": "g1"}, {"_id": "g2"}]))
    full = await api().list_guests(GuestQuery(limit=2))
    assert full.next_cursor == encode_cursor(2) and full.total is None
    router.get(GUESTS_URL).mock(return_value=json_response([{"_id": "g1"}]))
    short = await api().list_guests(GuestQuery(limit=2))
    assert short.next_cursor is None


async def test_list_never_hands_out_a_cursor_past_the_skip_limit(
    router: respx.MockRouter,
) -> None:
    router.get(GUESTS_URL).mock(return_value=json_response({"results": [{"_id": "g1"}]}))
    page = await api().list_guests(GuestQuery(limit=1, cursor=encode_cursor(MAX_SKIP)))
    assert page.next_cursor is None


async def test_a_cursor_that_was_not_made_here_is_refused(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    for bad in ("garbage", "c2tpcD0tMQ", encode_cursor(MAX_SKIP + 1)):
        with pytest.raises(PMSValidationError):
            await api().list_guests(GuestQuery(cursor=bad))
    assert route.call_count == 0
    assert decode_cursor(encode_cursor(5)) == 5


async def test_text_search_is_refused_and_nothing_is_requested(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({"results": []}))
    with pytest.raises(PMSValidationError) as caught:
        await api().list_guests(GuestQuery(search="alex"))
    assert str(caught.value) == guests.SEARCH_REFUSED
    assert "get_reservation" in str(caught.value) and "page by page" in str(caught.value)
    assert route.call_count == 0


async def test_an_unreadable_list_is_an_upstream_error(router: respx.MockRouter) -> None:
    router.get(GUESTS_URL).mock(return_value=json_response({"message": "no results key"}))
    with pytest.raises(PMSUpstreamError):
        await api().list_guests(GuestQuery())


async def test_items_without_an_id_are_skipped(router: respx.MockRouter) -> None:
    body = {"results": [{"fullName": "No Id"}, "text", {"_id": "g2"}], "total": 3}
    router.get(GUESTS_URL).mock(return_value=json_response(body))
    page = await api().list_guests(GuestQuery(limit=5))
    assert [g.id for g in page.items] == ["g2"]


async def test_list_error_text_is_not_passed_on(router: respx.MockRouter) -> None:
    router.get(GUESTS_URL).mock(return_value=json_response({"message": "private text"}, 400))
    with pytest.raises(PMSValidationError) as caught:
        await api().list_guests(GuestQuery())
    assert "private text" not in str(caught.value)


# ---- logs ----


async def test_logs_never_hold_guest_details(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(GUESTS_URL).mock(
        return_value=json_response(
            {
                "results": [
                    {"fullName": GUEST_NAME, "email": GUEST_EMAIL, "phone": GUEST_PHONE},
                    {"_id": "g9", "fullName": "x" * 9, "email": GUEST_EMAIL},
                ]
            }
        )
    )
    await api().list_guests(GuestQuery(limit=5))
    router.get(f"{GUESTS_URL}/{GUEST_ID}").mock(return_value=json_response(fixture("guest.json")))
    await api().get_guest(GUEST_ID)
    router.get(f"{GUESTS_URL}/bad").mock(
        return_value=json_response({"email": GUEST_EMAIL, "fullName": GUEST_NAME}, 500)
    )
    with pytest.raises(PMSUpstreamError):
        await api().get_guest("bad")
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in (GUEST_NAME, GUEST_EMAIL, GUEST_PHONE, *NEVER_SHOWN):
        assert private not in recorded


# ---- capabilities ----


def test_capabilities_and_wiring() -> None:
    assert Capability.GUESTS_READ in guests.CAPABILITIES and len(guests.CAPABILITIES) == 1
    assert DOMAIN_MODULES["guests"] == "guests"
    assert "guests" in discover_domains()
