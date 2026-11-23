"""Guesty payments of one reservation (read only), mapped to the canonical `Payment`.

Endpoint (see docs/providers/guesty/payments.md): `GET /payments?reservationId=`. The endpoint has
no field selection, so Guesty sends everything about each payment, including its payment method id
and, for card payments, the details of the card. The mapper reads only the fields named below and
drops the rest: it never reads, returns or logs card numbers, the last four digits, BINs, card
brands, payment method ids, processor references, receipts, bank details, refund notes or error
texts. The payment method is shown only as a plain word from a short fixed list. Shared helpers are
in `common.py`.
"""

import logging
import re
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import Capability, PMSUpstreamError
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    as_dict,
    as_list,
    datetime_of,
    money_of,
    number_of,
    quote_id,
    text_of,
)
from pmsbridge.pms.models import ExtraValue, Payment, PaymentStatus

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.PAYMENTS_READ})

MAX_PAYMENTS: Final = 200
_STATUS_PATTERN: Final = re.compile(r"^[A-Z][A-Z_]{0,39}$")

_STATUSES: Final[dict[str, PaymentStatus]] = {
    "PENDING": PaymentStatus.PENDING,
    "PENDING_AUTH": PaymentStatus.PENDING,
    "PROCESSING": PaymentStatus.PENDING,
    "SUCCEEDED": PaymentStatus.PAID,
    "FAILED": PaymentStatus.FAILED,
    "ERROR": PaymentStatus.FAILED,
    "CANCELLED": PaymentStatus.CANCELLED,
}
# Plain words only. Anything else (a card brand, a processor name, a token) is dropped. Guesty's
# payments list does not document a method, so this is only read if a `paymentMethod.method` is
# present.
_METHOD_WORDS: Final[dict[str, str]] = {
    "card": "card",
    "credit card": "card",
    "debit card": "card",
    "cash": "cash",
    "check": "check",
    "cheque": "check",
    "bank transfer": "bank transfer",
    "wire transfer": "bank transfer",
    "wire": "bank transfer",
}


def _method_word(item: dict[str, object]) -> str | None:
    """A plain payment method word from `paymentMethod.method`, or None."""
    method = as_dict(item.get("paymentMethod"))
    text = text_of(method.get("method")) if method is not None else None
    if text is None:
        return None
    return _METHOD_WORDS.get(text.lower().replace("_", " ").replace("-", " "))


def _refunded(item: dict[str, object]) -> Decimal:
    """The total of the refunds that were carried out (they have a `refundedAt` time)."""
    refunds = as_list(item.get("refunds")) or []
    total = Decimal(0)
    for entry in refunds:
        refund = as_dict(entry)
        if refund is None or datetime_of(refund.get("refundedAt"), naive_as_utc=True) is None:
            continue
        amount = number_of(refund.get("amount"))
        if amount is not None and amount > 0:
            total += Decimal(str(amount))
    return total


def _status(item: dict[str, object], amount: Decimal) -> tuple[PaymentStatus, str | None]:
    """The canonical status and, for a state with no canonical word, Guesty's own status."""
    raw = text_of(item.get("status"))
    mapped = _STATUSES.get(raw.upper()) if raw is not None else None
    if mapped is None:
        # Holds, security deposits and anything new: no canonical word, so say what Guesty said.
        known = raw.upper() if raw is not None and _STATUS_PATTERN.match(raw.upper()) else None
        return PaymentStatus.UNKNOWN, known
    if mapped is PaymentStatus.PAID:
        refunded = _refunded(item)
        if refunded > 0:
            return (
                PaymentStatus.REFUNDED if refunded >= amount else PaymentStatus.PARTIALLY_REFUNDED
            ), None
    return mapped, None


def map_payment(raw: object, reservation_id: str, envelope_currency: object) -> Payment | None:
    """The canonical payment, or None (logged without any detail) for an unusable item."""
    item = as_dict(raw)
    payment_id = text_of(item.get("_id")) if item is not None else None
    if item is None or payment_id is None:
        logger.warning("payment skipped: no usable id")
        return None
    money = money_of(item.get("amount"), item.get("currency") or envelope_currency)
    if money is None:
        logger.warning("payment skipped: no usable amount", extra={"payment_id": payment_id[:80]})
        return None
    status, pms_status = _status(item, money.amount)
    extras: dict[str, ExtraValue] = {} if pms_status is None else {"pms_status": pms_status}
    try:
        return Payment(
            id=payment_id,
            reservation_id=reservation_id,
            amount=money,
            status=status,
            method=_method_word(item),
            paid_at=datetime_of(item.get("paidAt"), naive_as_utc=True),
            due_at=datetime_of(item.get("shouldBePaidAt"), naive_as_utc=True),
            extras=extras,
        )
    except ValidationError:
        logger.warning(
            "payment skipped: could not be mapped", extra={"payment_id": payment_id[:80]}
        )
        return None


class GuestyPayments:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_reservation_payments(self, reservation_id: str) -> list[Payment]:
        cleaned = reservation_id.strip()
        quote_id(cleaned)  # refuse an empty or over-long id before any request
        response = await self._client.request("GET", "/payments", params={"reservationId": cleaned})
        body = response.json()
        envelope = as_dict(body)
        found = as_list(envelope.get("payments")) if envelope is not None else None
        if envelope is None or found is None:
            raise PMSUpstreamError(
                "Guesty returned a payments response that could not be understood."
            )
        currency = envelope.get("currency")
        payments = [
            payment
            for entry in found[:MAX_PAYMENTS]
            if (payment := map_payment(entry, cleaned, currency)) is not None
        ]
        return payments


def build(client: GuestyClient) -> GuestyPayments:
    return GuestyPayments(client)
