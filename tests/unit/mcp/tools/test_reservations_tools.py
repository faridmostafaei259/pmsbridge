"""The reservation tools through the MCP endpoint: with a fake provider and with Guesty adapters."""

from collections.abc import AsyncIterator, Iterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_reservation_helpers import (
    BY_IDS_URL,
    DOOR_CODE,
    GUEST_EMAIL,
    GUEST_NAME,
    REPORTS_URL,
    RESERVATION_ID,
    SEARCH_URL,
    fixture,
    json_response,
    make_client,
    mock_detail,
    mock_router,
)
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user

from pmsbridge.pms import registry
from pmsbridge.pms.base import (
    Capability,
    ConnectionRef,
    PMSNotFoundError,
    PMSProvider,
    Provider,
)
from pmsbridge.pms.guesty import reservations
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import (
    GuestSummary,
    Page,
    Reservation,
    ReservationQuery,
    ReservationStatus,
)
from pmsbridge.pms.testing import FakeProvider

TOOLS = ("list_reservations", "get_reservation")


def make_reservation(number: int) -> Reservation:
    return Reservation(
        id=f"r{number}",
        listing_id="l1",
        status=ReservationStatus.CONFIRMED,
        check_in=date(2027, 3, number + 1),
        check_out=date(2027, 3, number + 3),
        nights=2,
        guest=GuestSummary(name="Pat Invented", email="pat@example.test"),
    )


class FakeReservations:
    def __init__(self, items: list[Reservation]) -> None:
        self.items = items
        self.queries: list[ReservationQuery] = []

    async def list_reservations(self, query: ReservationQuery) -> Page[Reservation]:
        self.queries.append(query)
        return Page[Reservation](items=self.items, next_cursor=None)

    async def get_reservation(self, reservation_id: str) -> Reservation:
        for item in self.items:
            if item.id == reservation_id:
                return item
        raise PMSNotFoundError("Reservation not found.")


class ReservationsProvider(FakeProvider):
    def __init__(self, api: FakeReservations) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.RESERVATIONS_READ})

    @property
    def reservations(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""
        self.connection_id = ""

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    built.connection_id = await seed_connection("Beach houses")
    built.token, _ = await mint_token(user, connections=(built.connection_id,))
    yield built


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, api: FakeReservations) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return ReservationsProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(reservations.CAPABILITIES),
            {"reservations": reservations.build(make_client())},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        yield router


async def test_the_tools_are_listed_as_read_only(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    for name in TOOLS:
        tool = by_name[name]
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert "connection" in tool["inputSchema"]["properties"]
        assert tool["description"].strip()
    assert set(by_name["list_reservations"]["inputSchema"]["properties"]) == {
        "listing_id",
        "statuses",
        "check_in_from",
        "check_in_to",
        "check_out_from",
        "check_out_to",
        "created_from",
        "created_to",
        "search",
        "limit",
        "cursor",
        "connection",
    }
    assert by_name["get_reservation"]["inputSchema"]["required"] == ["reservation_id"]
    assert (
        "leave out the guest's contact details and the money"
        in (by_name["list_reservations"]["description"])
    )
    assert "contact details and balance" in by_name["get_reservation"]["description"]
    assert "add_reservation_note" not in by_name


async def test_the_tools_do_not_name_a_provider(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    for tool in listed:
        if tool["name"] in TOOLS:
            assert "guesty" not in str(tool).lower() and "hostaway" not in str(tool).lower()


async def test_tools_with_a_fake_provider(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeReservations([make_reservation(n) for n in range(3)])
    use_fake_provider(monkeypatch, api)
    listed = await world.call(
        "list_reservations",
        listing_id=" l1 ",
        statuses=["confirmed", "pending"],
        check_in_from="2027-03-01",
        check_in_to="2027-03-31",
        created_to="2027-01-31",
        search=" TST-1 ",
        limit=2,
    )
    assert listed["isError"] is False
    assert [item["id"] for item in listed["structuredContent"]["items"]] == ["r0", "r1", "r2"]
    query = api.queries[-1]
    assert query.limit == 2 and query.listing_id == "l1" and query.search == "TST-1"
    assert query.statuses == [ReservationStatus.CONFIRMED, ReservationStatus.PENDING]
    assert query.check_in_from == date(2027, 3, 1) and query.created_to == date(2027, 1, 31)
    one = await world.call("get_reservation", reservation_id="r1")
    assert one["structuredContent"]["check_in"] == "2027-03-02"
    assert one["structuredContent"]["guest"]["name"] == "Pat Invented"


async def test_blank_filters_are_left_out(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeReservations([])
    use_fake_provider(monkeypatch, api)
    await world.call("list_reservations", listing_id="  ", search="  ", statuses=[])
    query = api.queries[-1]
    assert query.listing_id is None and query.search is None and query.statuses is None
    assert query.limit == 25


async def test_errors_are_plain_messages(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_provider(monkeypatch, FakeReservations([]))
    missing = await world.call("get_reservation", reservation_id="nope")
    assert missing["isError"] is True and "not found" in result_text(missing).lower()
    too_long = await world.call("list_reservations", cursor="9" * 1001)
    assert too_long["isError"] is True and "cursor" in result_text(too_long)
    backwards = await world.call(
        "list_reservations", check_in_from="2027-03-10", check_in_to="2027-03-01"
    )
    assert backwards["isError"] is True and "check_in_to" in result_text(backwards)
    assert "Traceback" not in result_text(backwards)
    bad_date = await world.call("list_reservations", check_in_from="next tuesday")
    assert bad_date["isError"] is True
    bad_status = await world.call("list_reservations", statuses=["booked"])
    assert bad_status["isError"] is True


async def test_a_provider_without_reservations_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name, arguments in (
        ("list_reservations", {}),
        ("get_reservation", {"reservation_id": "x"}),
    ):
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


async def test_guesty_list_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(SEARCH_URL).mock(return_value=json_response(fixture("search_page.json")))
    result = await world.call(
        "list_reservations", statuses=["confirmed"], check_in_from="2027-03-01", limit=3
    )
    assert result["isError"] is False
    params = dict(route.calls.last.request.url.params)
    assert params["filter[status]"] == "confirmed" and params["limit"] == "3"
    assert params["filter[checkIn][gte]"] == "2027-03-01" and params["sort"] == "checkIn"
    page = result["structuredContent"]
    assert len(page["items"]) == 3 and page["next_cursor"]
    assert all(item["guest"] is None for item in page["items"])
    shown = str(result)
    for leaked in (DOOR_CODE, "keyCode", "doneBy", "EXT-INVENTED", "merchantOfRecord"):
        assert leaked not in shown
    nxt = await world.call("list_reservations", limit=3, cursor=page["next_cursor"])
    assert nxt["isError"] is False
    assert dict(route.calls.last.request.url.params)["skip"] == "3"


async def test_the_page_size_is_kept_within_bounds(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(SEARCH_URL).mock(return_value=json_response({"results": []}))
    await world.call("list_reservations", limit=100000)
    assert dict(route.calls.last.request.url.params)["limit"] == "100"
    await world.call("list_reservations", limit=0)
    assert dict(route.calls.last.request.url.params)["limit"] == "1"
    await world.call("list_reservations")
    assert dict(route.calls.last.request.url.params)["limit"] == "25"


async def test_guesty_get_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    mock_detail(guesty)
    result = await world.call("get_reservation", reservation_id=RESERVATION_ID)
    assert result["isError"] is False
    found = result["structuredContent"]
    assert found["guest"]["name"] == GUEST_NAME and found["guest"]["email"] == GUEST_EMAIL
    assert found["balance_due"]["currency"] == "EUR" and found["total_price"] is None
    shown = str(result)
    for leaked in (DOOR_CODE, "keyCode", "FAKE-CARD", "isUsingVCC", "moneyId"):
        assert leaked not in shown


async def test_guesty_get_still_answers_when_the_extras_fail(
    world: World, guesty: respx.MockRouter
) -> None:
    mock_detail(
        guesty, guest=json_response({}, 404), report=json_response({"message": "private"}, 500)
    )
    result = await world.call("get_reservation", reservation_id=RESERVATION_ID)
    assert result["isError"] is False
    assert result["structuredContent"]["guest"] is None
    assert result["structuredContent"]["balance_due"] is None
    assert result["structuredContent"]["confirmation_code"] == "TST-1001"


async def test_a_guesty_404_is_reported_plainly(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(BY_IDS_URL).mock(
        return_value=json_response({"message": "private upstream text"}, 404)
    )
    result = await world.call("get_reservation", reservation_id=RESERVATION_ID)
    assert result["isError"] is True
    assert "private upstream text" not in result_text(result)
    assert "not found" in result_text(result).lower()


async def test_a_guesty_failure_does_not_leak_upstream_text(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(SEARCH_URL).mock(return_value=json_response({"message": "private text"}, 400))
    guesty.get(REPORTS_URL).mock(return_value=json_response({}))
    result = await world.call("list_reservations")
    assert result["isError"] is True and "private text" not in result_text(result)


async def test_over_long_filters_are_refused_before_any_request(
    world: World, guesty: respx.MockRouter
) -> None:
    route = guesty.route().mock(return_value=json_response({"results": []}))
    for name in ("listing_id", "search"):
        refused = await world.call("list_reservations", **{name: "x" * 201})
        assert refused["isError"] is True
        assert name in result_text(refused) and "200" in result_text(refused)
    assert route.call_count == 0
    accepted = await world.call("list_reservations", search="x" * 200)
    assert accepted["isError"] is False and route.call_count == 1
