"""The calendar and pricing tools through the MCP endpoint, with a fake provider and with Guesty."""

from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_calendar_helpers import (
    CALENDAR_URL,
    LISTING_ID,
    LISTING_URL,
    PUT_URL,
    STAFF_NOTE,
    fake_days,
    fixture,
    json_response,
    make_client,
    mock_router,
    put_bodies,
)
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user

from pmsbridge.mcp import framework
from pmsbridge.pms import registry
from pmsbridge.pms.base import Capability, ConnectionRef, PMSProvider, Provider
from pmsbridge.pms.guesty import calendar, pricing
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import (
    CalendarDay,
    CalendarRange,
    CalendarUpdateRequest,
    Money,
    PricingSettings,
)
from pmsbridge.pms.testing import FakeProvider

TOOLS = ("get_calendar", "get_pricing_settings", "update_calendar")
CHANGE = {"start": "2030-12-20", "end": "2030-12-23", "price": 180, "currency": "EUR"}


class FakeCalendar:
    def __init__(self) -> None:
        self.reads: list[CalendarRange] = []
        self.requests: list[CalendarUpdateRequest] = []

    async def get_calendar(self, calendar_range: CalendarRange) -> list[CalendarDay]:
        self.reads.append(calendar_range)
        day = CalendarDay(
            listing_id=calendar_range.listing_id,
            day=calendar_range.start,
            available=True,
            price=Money(amount=Decimal("150"), currency="EUR"),
        )
        return [day]

    async def update_calendar(self, request: CalendarUpdateRequest) -> list[CalendarDay]:
        self.requests.append(request)
        return [
            CalendarDay(listing_id=request.listing_id, day=request.updates[0].start, available=True)
        ]


class FakePricing:
    async def get_pricing_settings(self, listing_id: str) -> PricingSettings:
        return PricingSettings(listing_id=listing_id, currency="EUR")


class CalendarProvider(FakeProvider):
    def __init__(self, api: FakeCalendar) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.CALENDAR_READ, Capability.CALENDAR_WRITE, Capability.PRICING_READ}
        )

    @property
    def calendar(self):  # type: ignore[no-untyped-def]
        return self._api

    @property
    def pricing(self):  # type: ignore[no-untyped-def]
        return FakePricing()


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""
        self.connection_id = ""
        self.read_only_id = ""
        self.loads: list[str] = []

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        arguments = {"connection": "Beach houses", **arguments}
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    built.connection_id = await seed_connection("Beach houses", writes_enabled=True)
    built.read_only_id = await seed_connection("Read only", writes_enabled=False)
    built.token, _ = await mint_token(user, connections=(built.connection_id, built.read_only_id))
    yield built


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, world: World, api: FakeCalendar) -> None:
    async def load(connection_id: str) -> PMSProvider:
        world.loads.append(connection_id)
        return CalendarProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        client = make_client()
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(calendar.CAPABILITIES | pricing.CAPABILITIES),
            {"calendar": calendar.build(client), "pricing": pricing.build(client)},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        # The listing record (EUR, base price 150) that the price checks anchor to.
        router.get(LISTING_URL).mock(return_value=json_response(fixture("pricing_full.json")))
        yield router


async def listed_tools(world: World) -> dict[str, Any]:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    return {tool["name"]: tool for tool in listed}


# ---- How the tools are published ----


async def test_reads_are_read_only_and_the_update_is_a_gated_change(world: World) -> None:
    tools = await listed_tools(world)
    for name in ("get_calendar", "get_pricing_settings"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    change = tools["update_calendar"]
    assert change["annotations"]["readOnlyHint"] is False
    assert change["annotations"]["destructiveHint"] is True
    for name in TOOLS:
        assert "connection" in tools[name]["inputSchema"]["properties"]
        assert (
            "guesty" not in str(tools[name]).lower() and "hostaway" not in str(tools[name]).lower()
        )


async def test_the_descriptions_tell_the_assistant_what_to_do(world: World) -> None:
    tools = await listed_tools(world)
    description = tools["update_calendar"]["description"]
    for phrase in (
        "confirm the exact dates and amounts",
        "At most 31 ranges",
        "listing's own currency",
    ):
        assert phrase in description
    assert "changes data" in description
    assert "at most 366 days" in tools["get_calendar"]["description"]
    schema = tools["update_calendar"]["inputSchema"]
    assert schema["properties"]["updates"]["maxItems"] == 31
    assert schema["required"] == ["listing_id", "updates"]


# ---- With a fake provider ----


async def test_get_calendar_passes_the_range(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call(
        "get_calendar", listing_id=" l1 ", start="2030-06-01", end="2030-06-03"
    )
    assert result["isError"] is False
    assert result["structuredContent"]["listing_id"] == "l1"
    assert result["structuredContent"]["days"][0]["price"]["currency"] == "EUR"
    assert api.reads == [
        CalendarRange(listing_id="l1", start=date(2030, 6, 1), end=date(2030, 6, 3))
    ]


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        ({"start": "2030-06-03", "end": "2030-06-01"}, "end must not be before start"),
        ({"start": "2030-01-01", "end": "2031-01-02"}, "at most 366 days"),
        ({"start": "soon", "end": "2030-06-01"}, ""),
        (
            {"listing_id": " ", "start": "2030-06-01", "end": "2030-06-02"},
            "listing id is not valid",
        ),
        ({"listing_id": "x" * 201, "start": "2030-06-01", "end": "2030-06-02"}, "listing id"),
    ],
)
async def test_get_calendar_refuses_a_bad_range_before_asking(
    world: World, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, str], words: str
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call("get_calendar", **{"listing_id": "l1", **arguments})
    assert result["isError"] is True and words in result_text(result)
    assert "Traceback" not in result_text(result) and api.reads == []


async def test_a_366_day_range_is_the_most_that_is_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call("get_calendar", listing_id="l1", start="2030-01-01", end="2030-12-31")
    assert result["isError"] is False and len(api.reads) == 1


async def test_get_pricing_settings_with_a_fake_provider(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_provider(monkeypatch, world, FakeCalendar())
    result = await world.call("get_pricing_settings", listing_id=" l1 ")
    assert result["isError"] is False
    assert result["structuredContent"]["listing_id"] == "l1"


async def test_update_builds_the_canonical_request(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call(
        "update_calendar",
        listing_id=" l1 ",
        updates=[
            {**CHANGE, "currency": "eur", "min_nights": 2},
            {"start": "2030-12-24", "end": "2030-12-24", "available": False},
        ],
    )
    assert result["isError"] is False, result_text(result)
    assert result["structuredContent"]["updates_applied"] == 2
    assert result["structuredContent"]["listing_id"] == "l1"
    (request,) = api.requests
    assert request.listing_id == "l1" and len(request.updates) == 2
    first = request.updates[0]
    assert first.price == Money(amount=Decimal("180"), currency="EUR") and first.min_nights == 2
    assert request.updates[1].available is False and request.updates[1].price is None


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"price": 180}, "currency is required with price"),
        ({"price": 180, "currency": "EURO"}, ""),
        ({"price": 180, "currency": "12$"}, "currency is required with price"),
        ({"min_nights": 2, "currency": "EUR"}, "currency is only used together with price"),
        ({"price": 0, "currency": "EUR"}, "greater than zero"),
        ({"price": 10.123, "currency": "EUR"}, "at most 2 decimal places"),
        ({"min_nights": 0}, ""),
        ({}, "at least one of price, min_nights or available"),
        ({"start": "2020-01-01", "end": "2020-01-02", "available": True}, "in the past"),
        ({"start": "2030-12-24", "end": "2030-12-20", "available": True}, "end must not be before"),
        ({"start": "2030-01-01", "end": "2031-06-01", "available": True}, "shorter than 366"),
        ({"surprise": 1, "available": True}, ""),
    ],
)
async def test_a_bad_range_is_refused_with_a_plain_message_and_nothing_is_sent(
    world: World, monkeypatch: pytest.MonkeyPatch, change: dict[str, object], words: str
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    updates = [{"start": "2030-12-20", "end": "2030-12-23", **change}]
    result = await world.call("update_calendar", listing_id="l1", updates=updates)
    assert result["isError"] is True and words in result_text(result)
    assert "Traceback" not in result_text(result) and api.requests == []


async def test_the_number_of_ranges_is_limited_to_31(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    one = {"start": "2030-12-20", "end": "2030-12-20", "available": True}
    too_many = await world.call("update_calendar", listing_id="l1", updates=[one] * 32)
    nothing = await world.call("update_calendar", listing_id="l1", updates=[])
    assert too_many["isError"] is True and nothing["isError"] is True
    assert api.requests == []
    fine = await world.call("update_calendar", listing_id="l1", updates=[one] * 31)
    assert fine["isError"] is False and len(api.requests[0].updates) == 31


# ---- The change switch and the write limit ----


async def test_a_change_is_refused_where_changes_are_not_allowed_and_nothing_is_called(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    refused = await world.call(
        "update_calendar", connection="Read only", listing_id="l1", updates=[CHANGE]
    )
    assert refused["isError"] is True
    assert "Changes are not allowed on the connection" in result_text(refused)
    assert world.loads == [] and api.requests == []  # no provider was even built
    reading = await world.call(
        "get_calendar",
        connection="Read only",
        listing_id="l1",
        start="2030-06-01",
        end="2030-06-02",
    )
    assert reading["isError"] is False  # reading still works on a read-only connection
    allowed = await world.call(
        "update_calendar", connection="Beach houses", listing_id="l1", updates=[CHANGE]
    )
    assert allowed["isError"] is False and len(api.requests) == 1


async def test_changes_have_their_own_limit_for_each_approval(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeCalendar()
    use_fake_provider(monkeypatch, world, api)
    for _ in range(framework.WRITES_BURST):
        done = await world.call("update_calendar", listing_id="l1", updates=[CHANGE])
        assert done["isError"] is False
    refused = await world.call("update_calendar", listing_id="l1", updates=[CHANGE])
    assert refused["isError"] is True and "changes too quickly" in result_text(refused)
    assert len(api.requests) == framework.WRITES_BURST
    still_reading = await world.call(
        "get_calendar", listing_id="l1", start="2030-06-01", end="2030-06-02"
    )
    assert still_reading["isError"] is False


async def test_a_provider_without_calendar_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name, arguments in (
        ("get_calendar", {"listing_id": "l", "start": "2030-06-01", "end": "2030-06-02"}),
        ("get_pricing_settings", {"listing_id": "l"}),
        ("update_calendar", {"listing_id": "l", "updates": [CHANGE]}),
    ):
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


# ---- With the Guesty adapters ----


async def test_guesty_get_calendar_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(CALENDAR_URL).mock(return_value=json_response(fixture("days_array.json")))
    result = await world.call(
        "get_calendar", listing_id=LISTING_ID, start="2030-06-01", end="2030-06-05"
    )
    assert result["isError"] is False
    days = result["structuredContent"]["days"]
    assert [day["day"] for day in days] == [f"2030-06-0{n}" for n in range(1, 6)]
    assert [day["available"] for day in days] == [True, False, False, False, True]
    assert days[1]["reservation_id"] == "6a3000000000000000000b01"
    assert days[2]["blocked_reason"] == "blocked"
    assert dict(route.calls.last.request.url.params)["listingIds"] == LISTING_ID
    shown = str(result)
    for leaked in (STAFF_NOTE, "gate code", "blockRefs", "isBasePrice"):
        assert leaked not in shown


async def test_guesty_pricing_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(LISTING_URL).mock(return_value=json_response(fixture("pricing_full.json")))
    result = await world.call("get_pricing_settings", listing_id=LISTING_ID)
    assert result["isError"] is False
    settings = result["structuredContent"]
    assert settings["currency"] == "EUR" and settings["weekly_discount_percent"] == 10.0
    assert settings["base_price"]["amount"] == "150"
    assert "cancellation" not in str(result) and "basePriceUSD" not in str(result)


async def test_guesty_update_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    window = fake_days("2030-12-19", "2030-12-27")
    guesty.get(CALENDAR_URL).mock(side_effect=[json_response(window), json_response(window)])
    puts = guesty.put(PUT_URL).mock(return_value=json_response("ok"))
    result = await world.call("update_calendar", listing_id=LISTING_ID, updates=[CHANGE])
    assert result["isError"] is False, result_text(result)
    assert put_bodies(puts) == [
        {"startDate": "2030-12-20", "endDate": "2030-12-23", "price": 180.0}
    ]
    changed = result["structuredContent"]
    assert changed["updates_applied"] == 1
    assert [day["day"] for day in changed["days"]] == [f"2030-12-{n}" for n in range(20, 24)]


async def test_guesty_refusal_reaches_the_assistant_and_sends_nothing(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(CALENDAR_URL).mock(return_value=json_response(fake_days("2030-12-19", "2030-12-27")))
    puts = guesty.put(PUT_URL).mock(return_value=json_response("ok"))
    result = await world.call(
        "update_calendar", listing_id=LISTING_ID, updates=[{**CHANGE, "price": 5}]
    )
    assert result["isError"] is True and "looks like a mistake" in result_text(result)
    assert "change it in Guesty directly" in result_text(result)
    assert puts.call_count == 0


async def test_guesty_partial_failure_message_fits_and_reaches_the_assistant(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(CALENDAR_URL).mock(return_value=json_response(fake_days("2030-12-19", "2030-12-27")))
    puts = guesty.put(PUT_URL).mock(
        side_effect=[json_response("ok"), json_response({"message": "private"}, 500)]
    )
    updates = [CHANGE, {"start": "2030-12-24", "end": "2030-12-25", "min_nights": 2}, CHANGE]
    result = await world.call("update_calendar", listing_id=LISTING_ID, updates=updates)
    text = result_text(result)
    assert result["isError"] is True and puts.call_count == 2
    assert "Range 2 of 3 (2030-12-24 to 2030-12-25) did not complete" in text
    assert "Range 1 was applied. Range 3 was not sent." in text
    assert text.endswith("the outcome of range 2 is unknown.")
    assert "private" not in text and "..." not in text  # whole message, never cut short


async def test_guesty_failures_do_not_leak_upstream_text(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(CALENDAR_URL).mock(return_value=json_response({"message": "private text"}, 400))
    result = await world.call(
        "get_calendar", listing_id=LISTING_ID, start="2030-06-01", end="2030-06-02"
    )
    assert result["isError"] is True and "private text" not in result_text(result)
