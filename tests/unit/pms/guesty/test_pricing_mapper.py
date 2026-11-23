"""Guesty pricing settings: the mapper, the discount factor edge cases and the request."""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import respx
from guesty_calendar_helpers import (
    LISTING_ID,
    LISTING_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms.base import Capability, PMSNotFoundError, PMSUpstreamError, PMSValidationError
from pmsbridge.pms.guesty import pricing
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def test_the_module_is_wired_into_the_provider() -> None:
    assert {Capability.PRICING_READ} == pricing.CAPABILITIES
    assert DOMAIN_MODULES["pricing"] == "pricing"
    assert "pricing" in discover_domains()


def test_every_setting_is_mapped_with_the_listing_currency() -> None:
    settings = pricing.map_pricing(LISTING_ID, fixture("pricing_full.json"))
    assert settings.currency == "EUR"
    amounts = {
        "base_price": "150",
        "weekend_base_price": "180",
        "cleaning_fee": "60",
        "extra_guest_fee": "15.5",
        "security_deposit": "200",
    }
    for name, amount in amounts.items():
        money = getattr(settings, name)
        assert (money.amount, money.currency) == (Decimal(amount), "EUR"), name
    assert settings.guests_included == 2
    assert settings.weekly_discount_percent == 10.0 and settings.monthly_discount_percent == 20.0
    assert (settings.min_nights, settings.max_nights) == (2, 30)
    assert settings.extras == {}


@pytest.mark.parametrize(
    ("factor", "percent"),
    [
        (1, 0.0),
        (0.8, 20.0),
        (0.875, 12.5),
        (0.333, 66.7),
        (0.9999, 0.01),
        (0.001, 99.9),
        (0, None),
        (-0.1, None),
        (1.5, None),
        ("0.9", None),
        (True, None),
        (None, None),
        (float("nan"), None),
    ],
)
def test_the_discount_is_the_complement_of_the_factor(
    factor: object, percent: float | None
) -> None:
    assert pricing.discount_percent(factor) == percent


def test_values_that_are_not_plain_amounts_are_left_out() -> None:
    settings = pricing.map_pricing(LISTING_ID, fixture("pricing_odd.json"))
    assert settings.currency == "GBP"
    assert settings.base_price is None  # negative
    assert settings.weekend_base_price is None  # a string
    assert settings.cleaning_fee is None  # Guesty's per-channel object, not a plain number
    assert settings.extra_guest_fee is not None and settings.extra_guest_fee.amount == 0
    assert settings.guests_included is None and settings.security_deposit is None
    assert settings.weekly_discount_percent is None and settings.monthly_discount_percent is None
    assert settings.min_nights is None and settings.max_nights is None


@pytest.mark.parametrize(
    "body",
    [{"prices": {"basePrice": 100}}, {"prices": {"currency": "euro"}}, {}, [], "text", None],
)
def test_a_listing_without_a_currency_or_not_an_object_is_an_upstream_error(body: object) -> None:
    with pytest.raises(PMSUpstreamError):
        pricing.map_pricing(LISTING_ID, body)


async def test_the_request_names_only_the_price_and_term_fields(router: respx.MockRouter) -> None:
    route = router.get(LISTING_URL).mock(return_value=json_response(fixture("pricing_full.json")))
    settings = await pricing.build(make_client()).get_pricing_settings(f"  {LISTING_ID} ")
    assert settings.listing_id == LISTING_ID
    assert dict(route.calls.last.request.url.params)["fields"] == "_id prices terms"


async def test_the_id_is_quoted_and_an_empty_one_refused(router: respx.MockRouter) -> None:
    route = router.get(url__startswith=LISTING_URL.rsplit("/", 1)[0]).mock(
        return_value=json_response(fixture("pricing_full.json"))
    )
    await pricing.build(make_client()).get_pricing_settings("a/b c")
    assert route.calls.last.request.url.raw_path.split(b"?")[0].endswith(b"/listings/a%2Fb%20c")
    with pytest.raises(PMSValidationError):
        await pricing.build(make_client()).get_pricing_settings("  ")


async def test_a_missing_listing_is_a_plain_not_found(router: respx.MockRouter) -> None:
    router.get(LISTING_URL).mock(return_value=json_response({"message": "private"}, 404))
    with pytest.raises(PMSNotFoundError) as caught:
        await pricing.build(make_client()).get_pricing_settings(LISTING_ID)
    assert "private" not in caught.value.message
