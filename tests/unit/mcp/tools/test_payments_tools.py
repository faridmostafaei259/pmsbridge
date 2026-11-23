"""The payments tool through the MCP endpoint: with a fake provider and with the Guesty adapter."""

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_owner_helpers import (
    PAYMENT_NEVER_SHOWN,
    PAYMENTS_URL,
    RESERVATION_ID,
    fixture,
    json_response,
    make_client,
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
from pmsbridge.pms.guesty import financials
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import Money, Payment, PaymentStatus
from pmsbridge.pms.testing import FakeProvider

TOOL = "list_reservation_payments"


class FakePayments:
    def __init__(self, items: list[Payment]) -> None:
        self.items = items
        self.asked: list[str] = []

    async def list_reservation_payments(self, reservation_id: str) -> list[Payment]:
        self.asked.append(reservation_id)
        if reservation_id == "missing":
            raise PMSNotFoundError("Reservation not found.")
        return self.items


class PaymentsProvider(FakeProvider):
    def __init__(self, api: FakePayments) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.PAYMENTS_READ})

    @property
    def payments(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    connection_id = await seed_connection("Beach houses")
    built.token, _ = await mint_token(user, connections=(connection_id,))
    yield built


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, api: FakePayments) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return PaymentsProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(financials.CAPABILITIES),
            {"payments": financials.build(make_client())},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        yield router


async def test_the_tool_is_listed_as_read_only(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    tool = {t["name"]: t for t in listed}[TOOL]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert set(tool["inputSchema"]["properties"]) == {"reservation_id", "connection"}
    assert tool["inputSchema"]["required"] == ["reservation_id"]
    assert "never shown" in tool["description"]
    assert "guesty" not in str(tool).lower() and "hostaway" not in str(tool).lower()


async def test_the_tool_with_a_fake_provider(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    payment = Payment(
        id="p1",
        reservation_id="r1",
        amount=Money(amount=Decimal("10.50"), currency="EUR"),
        status=PaymentStatus.PAID,
        method="card",
    )
    api = FakePayments([payment])
    use_fake_provider(monkeypatch, api)
    result = await world.call(TOOL, reservation_id=" r1 ")
    assert result["isError"] is False
    content = result["structuredContent"]
    assert content["reservation_id"] == "r1"
    assert content["payments"][0]["status"] == "paid" and content["payments"][0]["method"] == "card"
    assert content["payments"][0]["amount"]["currency"] == "EUR"
    assert api.asked == [" r1 "]


async def test_errors_are_plain_messages(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_provider(monkeypatch, FakePayments([]))
    missing = await world.call(TOOL, reservation_id="missing")
    assert missing["isError"] is True and "not found" in result_text(missing).lower()
    long_id = await world.call(TOOL, reservation_id="x" * 201)
    assert long_id["isError"] is True and "200" in result_text(long_id)


async def test_a_provider_without_payments_refuses_the_tool(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    refused = await world.call(TOOL, reservation_id="x")
    assert refused["isError"] is True and "does not support" in result_text(refused)


async def test_guesty_end_to_end_shows_no_card_or_bank_details(
    world: World, guesty: respx.MockRouter
) -> None:
    route = guesty.get(PAYMENTS_URL).mock(
        return_value=json_response(fixture("payments", "payments.json"))
    )
    result = await world.call(TOOL, reservation_id=RESERVATION_ID)
    assert result["isError"] is False
    assert dict(route.calls.last.request.url.params) == {"reservationId": RESERVATION_ID}
    content = result["structuredContent"]
    assert content["reservation_id"] == RESERVATION_ID and len(content["payments"]) == 4
    assert content["payments"][0]["status"] == "partially_refunded"
    shown = str(result)
    for leaked in PAYMENT_NEVER_SHOWN:
        assert leaked not in shown


async def test_a_guesty_404_is_reported_plainly(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(PAYMENTS_URL).mock(
        return_value=json_response({"error": {"message": "private upstream text"}}, 404)
    )
    result = await world.call(TOOL, reservation_id=RESERVATION_ID)
    assert result["isError"] is True
    assert "private upstream text" not in result_text(result)
    assert "not found" in result_text(result).lower()
