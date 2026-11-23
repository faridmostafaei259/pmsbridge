"""The Guesty payments adapter through the real client, with respx as the only network."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import respx
from guesty_owner_helpers import (
    PAYMENT_NEVER_SHOWN,
    PAYMENTS_URL,
    RESERVATION_ID,
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
from pmsbridge.pms.guesty import financials
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import PaymentStatus


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def api() -> financials.GuestyPayments:
    return financials.build(make_client())


def one(**fields: object) -> dict[str, object]:
    return {"_id": "p1", "amount": 10, "currency": "usd", **fields}


async def payment_of(router: respx.MockRouter, **fields: object):  # type: ignore[no-untyped-def]
    router.get(PAYMENTS_URL).mock(return_value=json_response({"payments": [one(**fields)]}))
    result = await api().list_reservation_payments(RESERVATION_ID)
    assert len(result) == 1
    return result[0]


async def test_the_fixture_is_mapped_and_nothing_private_is_returned(
    router: respx.MockRouter,
) -> None:
    route = router.get(PAYMENTS_URL).mock(
        return_value=json_response(fixture("payments", "payments.json"))
    )
    payments = await api().list_reservation_payments(RESERVATION_ID)
    assert dict(route.calls.last.request.url.params) == {"reservationId": RESERVATION_ID}
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"
    first, second, third, fourth = payments
    assert first.id == "6a6000000000000000000d01" and first.reservation_id == RESERVATION_ID
    assert first.amount.amount == Decimal("400") and first.amount.currency == "EUR"
    assert first.status is PaymentStatus.PARTIALLY_REFUNDED and first.method == "card"
    assert first.paid_at == datetime(2026, 3, 1, 10, 0, 5, tzinfo=UTC)
    assert first.due_at == datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)
    assert second.status is PaymentStatus.PENDING and second.amount.amount == Decimal("250.5")
    assert second.paid_at is None and second.method is None
    assert second.due_at == datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    assert third.status is PaymentStatus.FAILED and third.method == "bank transfer"
    assert third.amount.currency == "EUR"  # from the response, as the payment has none
    assert fourth.status is PaymentStatus.UNKNOWN
    assert fourth.extras == {"pms_status": "AUTHORIZATION_HOLD_SUCCEEDED"}
    shown = " ".join(p.model_dump_json() for p in payments)
    for private in PAYMENT_NEVER_SHOWN:
        assert private not in shown
    assert all(p.extras == {} for p in payments[:3])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PENDING", PaymentStatus.PENDING),
        ("PENDING_AUTH", PaymentStatus.PENDING),
        ("PROCESSING", PaymentStatus.PENDING),
        ("SUCCEEDED", PaymentStatus.PAID),
        ("succeeded", PaymentStatus.PAID),
        ("FAILED", PaymentStatus.FAILED),
        ("ERROR", PaymentStatus.FAILED),
        ("CANCELLED", PaymentStatus.CANCELLED),
        ("SECURITY_DEPOSIT_SUCCEEDED", PaymentStatus.UNKNOWN),
        ("BRAND_NEW_STATE", PaymentStatus.UNKNOWN),
        (None, PaymentStatus.UNKNOWN),
        (7, PaymentStatus.UNKNOWN),
    ],
)
async def test_status_mapping(
    router: respx.MockRouter, raw: object, expected: PaymentStatus
) -> None:
    assert (await payment_of(router, status=raw)).status is expected


async def test_an_unknown_status_is_kept_only_when_it_is_a_plain_token(
    router: respx.MockRouter,
) -> None:
    assert (await payment_of(router, status="NEW_STATE")).extras == {"pms_status": "NEW_STATE"}
    assert (await payment_of(router, status="pm_fake_555 4242")).extras == {}
    assert (await payment_of(router, status="x" * 80)).extras == {}


async def test_refunds_decide_refunded_and_partially_refunded(router: respx.MockRouter) -> None:
    done = {"amount": 4, "refundedAt": "2026-01-01T00:00:00Z"}
    pending = {"amount": 99, "status": "PENDING"}
    full = await payment_of(
        router, status="SUCCEEDED", amount=10, refunds=[{**done, "amount": 6}, done, pending]
    )
    assert full.status is PaymentStatus.REFUNDED
    part = await payment_of(router, status="SUCCEEDED", amount=10, refunds=[done, pending])
    assert part.status is PaymentStatus.PARTIALLY_REFUNDED
    none = await payment_of(router, status="SUCCEEDED", amount=10, refunds=[pending, "x", None])
    assert none.status is PaymentStatus.PAID
    other = await payment_of(router, status="FAILED", refunds=[done])
    assert other.status is PaymentStatus.FAILED


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("CARD", "card"),
        ("credit_card", "card"),
        ("cash", "cash"),
        ("bank-transfer", "bank transfer"),
        ("Cheque", "check"),
        ("VisaFake", None),
        ("pm_fake_555", None),
        ("4242 4242", None),
        ("", None),
        (5, None),
    ],
)
async def test_the_method_is_a_plain_word_or_nothing(
    router: respx.MockRouter, method: object, expected: str | None
) -> None:
    payment = await payment_of(router, paymentMethod={"method": method, "last4": "9917"})
    assert payment.method == expected
    assert "9917" not in payment.model_dump_json()


async def test_a_payment_method_that_is_not_an_object_gives_no_method(
    router: respx.MockRouter,
) -> None:
    for value in ("card", ["card"], None, 5):
        assert (await payment_of(router, paymentMethod=value)).method is None
    assert (await payment_of(router, paymentMethodId="pm_fake_555")).method is None


async def test_amount_and_currency_rules(router: respx.MockRouter) -> None:
    body = {
        "currency": "gbp",
        "payments": [
            {"_id": "a", "amount": 12.34},
            {"_id": "b", "amount": 1, "currency": "EUR"},
            {"_id": "c", "amount": "12"},
            {"_id": "d", "amount": True},
            {"_id": "e"},
            {"_id": "f", "amount": 5, "currency": "EURO"},
            {"amount": 5, "currency": "EUR"},
            "text",
        ],
    }
    router.get(PAYMENTS_URL).mock(return_value=json_response(body))
    payments = await api().list_reservation_payments(RESERVATION_ID)
    assert [(p.id, p.amount.amount, p.amount.currency) for p in payments] == [
        ("a", Decimal("12.34"), "GBP"),
        ("b", Decimal(1), "EUR"),
    ]


async def test_times_must_be_instants(router: respx.MockRouter) -> None:
    payment = await payment_of(router, paidAt="2026-05-01", shouldBePaidAt="not a time")
    assert payment.paid_at is None and payment.due_at is None
    naive = await payment_of(router, paidAt="2026-05-01T08:00:00")
    assert naive.paid_at == datetime(2026, 5, 1, 8, tzinfo=UTC)


async def test_an_empty_payments_list_is_an_empty_result(router: respx.MockRouter) -> None:
    router.get(PAYMENTS_URL).mock(return_value=json_response({"payments": []}))
    assert await api().list_reservation_payments(RESERVATION_ID) == []


async def test_the_number_of_payments_is_capped(router: respx.MockRouter) -> None:
    many = [one(_id=f"p{i}") for i in range(financials.MAX_PAYMENTS + 20)]
    router.get(PAYMENTS_URL).mock(return_value=json_response({"payments": many}))
    assert len(await api().list_reservation_payments(RESERVATION_ID)) == financials.MAX_PAYMENTS


async def test_an_unreadable_response_is_an_upstream_error(router: respx.MockRouter) -> None:
    for body in ({"message": "no payments key"}, [], "text", {"payments": "x"}):
        router.get(PAYMENTS_URL).mock(return_value=json_response(body))
        with pytest.raises(PMSUpstreamError):
            await api().list_reservation_payments(RESERVATION_ID)


async def test_the_reservation_id_is_checked_and_trimmed(router: respx.MockRouter) -> None:
    route = router.get(PAYMENTS_URL).mock(return_value=json_response({"payments": [one()]}))
    result = await api().list_reservation_payments(f"  {RESERVATION_ID} ")
    assert dict(route.calls.last.request.url.params)["reservationId"] == RESERVATION_ID
    assert result[0].reservation_id == RESERVATION_ID
    count = route.call_count
    for bad in ("", "  ", "x" * 201):
        with pytest.raises(PMSValidationError):
            await api().list_reservation_payments(bad)
    assert route.call_count == count


async def test_a_404_is_not_found_without_upstream_text(router: respx.MockRouter) -> None:
    router.get(PAYMENTS_URL).mock(
        return_value=json_response({"error": {"message": "private upstream text"}}, 404)
    )
    with pytest.raises(PMSNotFoundError) as caught:
        await api().list_reservation_payments(RESERVATION_ID)
    assert "private upstream text" not in str(caught.value)


async def test_logs_never_hold_payment_details(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = fixture("payments", "payments.json")
    assert isinstance(body, dict)
    body["payments"] = [
        *body["payments"],
        {"amount": 5, "paymentMethodId": "pm_fake_555", "paymentMethod": {"last4": "9917"}},
        {"_id": "bad", "paymentMethodId": "pm_fake_555", "status": "SUCCEEDED"},
    ]
    router.get(PAYMENTS_URL).mock(return_value=json_response(body))
    await api().list_reservation_payments(RESERVATION_ID)
    router.get(PAYMENTS_URL).mock(
        return_value=json_response({"paymentMethodId": "pm_fake_555", "last4": "9917"}, 500)
    )
    with pytest.raises(PMSUpstreamError):
        await api().list_reservation_payments(RESERVATION_ID)
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in PAYMENT_NEVER_SHOWN:
        assert private not in recorded


def test_capabilities_and_wiring() -> None:
    assert Capability.PAYMENTS_READ in financials.CAPABILITIES and len(financials.CAPABILITIES) == 1
    assert DOMAIN_MODULES["financials"] == "payments"
    assert "financials" in discover_domains()
