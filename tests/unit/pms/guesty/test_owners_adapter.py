"""The Guesty owner adapter through the real client, with respx as the only network."""

import logging
from collections.abc import AsyncIterator

import pytest
import respx
from guesty_owner_helpers import (
    OWNER_EMAIL,
    OWNER_ID,
    OWNER_NAME,
    OWNER_NEVER_REQUESTED,
    OWNER_NEVER_SHOWN,
    OWNER_PHONE,
    OWNERS_URL,
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
from pmsbridge.pms.guesty import owners
from pmsbridge.pms.guesty.common import MAX_SKIP, encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import PageRequest


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


def api() -> owners.GuestyOwners:
    return owners.build(make_client())


# ---- get ----


async def test_get_maps_a_full_owner_and_drops_payout_details(router: respx.MockRouter) -> None:
    route = router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response(fixture("owners", "owner.json"))
    )
    owner = await api().get_owner(OWNER_ID)
    assert owner.id == OWNER_ID and owner.full_name == OWNER_NAME
    assert owner.email == OWNER_EMAIL and owner.phone == OWNER_PHONE
    assert owner.listing_ids == ["6a1000000000000000000c01", "6a1000000000000000000c02"]
    assert owner.extras == {}
    shown = owner.model_dump_json()
    for private in OWNER_NEVER_SHOWN:
        assert private not in shown
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_get_asks_only_for_the_mapped_fields(router: respx.MockRouter) -> None:
    route = router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response(fixture("owners", "owner.json"))
    )
    await api().get_owner(OWNER_ID)
    fields = query_of(route)["fields"].split(" ")
    assert fields == ["_id", "fullName", "firstName", "lastName", "email", "phone", "listings"]
    for name in OWNER_NEVER_REQUESTED:
        assert name not in fields


async def test_get_maps_a_minimal_owner_and_builds_a_name_from_parts(
    router: respx.MockRouter,
) -> None:
    router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(return_value=json_response({"_id": OWNER_ID}))
    owner = await api().get_owner(OWNER_ID)
    assert owner.full_name == "Unknown" and owner.email is None and owner.listing_ids == []
    router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response({"_id": OWNER_ID, "firstName": "Jo", "lastName": " Doe "})
    )
    assert (await api().get_owner(OWNER_ID)).full_name == "Jo Doe"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["a", "b", "a", " "], ["a", "b"]),
        ([{"_id": "x"}, "y", {"nickname": "z"}, 5], ["x", "y"]),
        ({"unexpected": "shape"}, []),
        ("single", []),
        (None, []),
    ],
)
async def test_listing_ids_are_read_only_from_a_list(
    router: respx.MockRouter, value: object, expected: list[str]
) -> None:
    router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response({"_id": OWNER_ID, "listings": value})
    )
    assert (await api().get_owner(OWNER_ID)).listing_ids == expected


async def test_get_quotes_the_id_and_refuses_bad_ids_before_any_request(
    router: respx.MockRouter,
) -> None:
    route = router.get(url__startswith=f"{OWNERS_URL}/").mock(
        return_value=json_response({"_id": "x"})
    )
    await api().get_owner("a/b?c d")
    assert route.calls.last.request.url.raw_path.startswith(b"/v1/owners/a%2Fb%3Fc%20d")
    count = route.call_count
    for bad in ("", "  ", "x" * 201):
        with pytest.raises(PMSValidationError):
            await api().get_owner(bad)
    assert route.call_count == count


async def test_a_404_is_not_found_without_upstream_text(router: respx.MockRouter) -> None:
    router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response({"error": {"message": "private upstream text"}}, 404)
    )
    with pytest.raises(PMSNotFoundError) as caught:
        await api().get_owner(OWNER_ID)
    assert "private upstream text" not in str(caught.value)


async def test_a_body_without_an_id_is_an_upstream_error(router: respx.MockRouter) -> None:
    for body in ({"fullName": "X"}, ["not", "a", "dict"], "text"):
        router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(return_value=json_response(body))
        with pytest.raises(PMSUpstreamError):
            await api().get_owner(OWNER_ID)


# ---- list ----


async def test_list_sends_the_documented_parameters(router: respx.MockRouter) -> None:
    route = router.get(OWNERS_URL).mock(return_value=json_response({"results": []}))
    page = await api().list_owners(PageRequest(limit=10))
    assert page.items == [] and page.next_cursor is None and page.total is None
    assert query_of(route) == {
        "fields": owners.OWNER_FIELDS,
        "limit": "10",
        "skip": "0",
        "sort": "_id",
        "pagination": "true",
    }
    for name in OWNER_NEVER_REQUESTED:
        assert name not in owners.OWNER_FIELDS.split(" ")


async def test_list_maps_items_and_the_total(router: respx.MockRouter) -> None:
    router.get(OWNERS_URL).mock(return_value=json_response(fixture("owners", "list_page.json")))
    page = await api().list_owners(PageRequest(limit=3))
    first, second, third = page.items
    assert first.full_name == OWNER_NAME and first.email == OWNER_EMAIL
    assert second.full_name == "Jules Invented" and second.listing_ids == [
        "6a1000000000000000000c03"
    ]
    assert third.full_name == "Unknown" and third.listing_ids == []
    assert page.total == 7 and page.next_cursor == encode_cursor(3)
    shown = page.model_dump_json()
    for private in OWNER_NEVER_SHOWN:
        assert private not in shown


async def test_list_cursor_continues_by_skip_and_ends_on_the_last_page(
    router: respx.MockRouter,
) -> None:
    route = router.get(OWNERS_URL).mock(
        return_value=json_response(fixture("owners", "list_page.json"))
    )
    page = await api().list_owners(PageRequest(limit=3))
    await api().list_owners(PageRequest(limit=3, cursor=page.next_cursor))
    assert query_of(route)["skip"] == "3"
    body = {"results": [{"_id": "o1"}, {"_id": "o2"}], "totalCount": 2}
    router.get(OWNERS_URL).mock(return_value=json_response(body))
    last = await api().list_owners(PageRequest(limit=2))
    assert last.next_cursor is None and last.total == 2


async def test_a_bare_array_pages_by_full_pages(router: respx.MockRouter) -> None:
    router.get(OWNERS_URL).mock(return_value=json_response([{"_id": "o1"}, {"_id": "o2"}]))
    full = await api().list_owners(PageRequest(limit=2))
    assert full.next_cursor == encode_cursor(2) and full.total is None
    router.get(OWNERS_URL).mock(return_value=json_response([{"_id": "o1"}]))
    assert (await api().list_owners(PageRequest(limit=2))).next_cursor is None


async def test_list_never_hands_out_a_cursor_past_the_skip_limit(
    router: respx.MockRouter,
) -> None:
    router.get(OWNERS_URL).mock(return_value=json_response({"results": [{"_id": "o1"}]}))
    page = await api().list_owners(PageRequest(limit=1, cursor=encode_cursor(MAX_SKIP)))
    assert page.next_cursor is None


async def test_a_cursor_that_was_not_made_here_is_refused(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    for bad in ("garbage", "c2tpcD0tMQ", encode_cursor(MAX_SKIP + 1)):
        with pytest.raises(PMSValidationError):
            await api().list_owners(PageRequest(cursor=bad))
    assert route.call_count == 0


async def test_an_unreadable_list_is_an_upstream_error(router: respx.MockRouter) -> None:
    router.get(OWNERS_URL).mock(return_value=json_response({"message": "no results key"}))
    with pytest.raises(PMSUpstreamError):
        await api().list_owners(PageRequest())


async def test_items_without_an_id_are_skipped(router: respx.MockRouter) -> None:
    body = {"results": [{"fullName": "No Id"}, "text", {"_id": "o2"}], "totalCount": 3}
    router.get(OWNERS_URL).mock(return_value=json_response(body))
    page = await api().list_owners(PageRequest(limit=5))
    assert [o.id for o in page.items] == ["o2"]


async def test_list_error_text_is_not_passed_on(router: respx.MockRouter) -> None:
    router.get(OWNERS_URL).mock(
        return_value=json_response({"error": {"message": "private text"}}, 400)
    )
    with pytest.raises(PMSValidationError) as caught:
        await api().list_owners(PageRequest())
    assert "private text" not in str(caught.value)


# ---- logs ----


async def test_logs_never_hold_owner_details(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(OWNERS_URL).mock(
        return_value=json_response(
            {
                "results": [
                    {"fullName": OWNER_NAME, "email": OWNER_EMAIL, "phone": OWNER_PHONE},
                    {"_id": "o9", "fullName": "x" * 9, "email": OWNER_EMAIL},
                ]
            }
        )
    )
    await api().list_owners(PageRequest(limit=5))
    router.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response(fixture("owners", "owner.json"))
    )
    await api().get_owner(OWNER_ID)
    router.get(f"{OWNERS_URL}/bad").mock(
        return_value=json_response(
            {"email": OWNER_EMAIL, "payoutMethods": ["FAKE-IBAN-GB00-0000"]}, 500
        )
    )
    with pytest.raises(PMSUpstreamError):
        await api().get_owner("bad")
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in (OWNER_NAME, OWNER_EMAIL, OWNER_PHONE, *OWNER_NEVER_SHOWN):
        assert private not in recorded


# ---- capabilities ----


def test_capabilities_and_wiring() -> None:
    assert Capability.OWNERS_READ in owners.CAPABILITIES and len(owners.CAPABILITIES) == 1
    assert DOMAIN_MODULES["owners"] == "owners"
    assert "owners" in discover_domains()
