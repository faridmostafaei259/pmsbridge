"""Guesty pricing settings (read only): a listing's standing prices, fees and discounts.

Endpoint (see docs/providers/guesty/calendar.md): `GET /listings/{id}` with `fields` set to the
price settings and the stay-length limits (`prices`, `terms`), so nothing else is requested.
Day-by-day prices are in the calendar. Changing these settings is not available: Guesty's
financials endpoint is not used.
"""

import logging
import re
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import Capability, PMSUpstreamError
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import as_dict, count_of, money_of, number_of, quote_id, text_of
from pmsbridge.pms.models import Money, PricingSettings

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.PRICING_READ})

FIELDS: Final = "_id prices terms"
_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")


def _fee(prices: dict[str, object], key: str, currency: str) -> Money | None:
    """A plain non-negative amount. Anything else (such as a per-channel object) is left out."""
    amount = number_of(prices.get(key))
    return money_of(amount, currency) if amount is not None and amount >= 0 else None


def discount_percent(factor: object) -> float | None:
    """The discount for a Guesty price factor: 0.8 means 20 percent off. Only 0 < factor <= 1."""
    value = number_of(factor)
    if value is None or not 0 < value <= 1:
        return None
    return round((1 - value) * 100, 2)


def map_pricing(listing_id: str, raw: object) -> PricingSettings:
    item = as_dict(raw)
    if item is None:
        raise PMSUpstreamError("Guesty returned a listing that could not be understood.")
    prices = as_dict(item.get("prices")) or {}
    terms = as_dict(item.get("terms")) or {}
    currency = (text_of(prices.get("currency")) or "").upper()
    if not _CURRENCY_PATTERN.match(currency):
        raise PMSUpstreamError("Guesty did not say which currency this listing's prices use.")
    try:
        return PricingSettings(
            listing_id=listing_id,
            currency=currency,
            base_price=_fee(prices, "basePrice", currency),
            weekend_base_price=_fee(prices, "weekendBasePrice", currency),
            cleaning_fee=_fee(prices, "cleaningFee", currency),
            extra_guest_fee=_fee(prices, "extraPersonFee", currency),
            guests_included=count_of(prices.get("guestsIncludedInRegularFee")),
            security_deposit=_fee(prices, "securityDepositFee", currency),
            weekly_discount_percent=discount_percent(prices.get("weeklyPriceFactor")),
            monthly_discount_percent=discount_percent(prices.get("monthlyPriceFactor")),
            min_nights=count_of(terms.get("minNights")),
            max_nights=count_of(terms.get("maxNights")),
        )
    except ValidationError:
        logger.warning("pricing could not be mapped", extra={"listing_id": listing_id[:80]})
        raise PMSUpstreamError("Guesty returned prices that could not be understood.") from None


class GuestyPricing:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def get_pricing_settings(self, listing_id: str) -> PricingSettings:
        response = await self._client.request(
            "GET", f"/listings/{quote_id(listing_id)}", params={"fields": FIELDS}
        )
        return map_pricing(listing_id.strip(), response.json())


def build(client: GuestyClient) -> GuestyPricing:
    return GuestyPricing(client)
