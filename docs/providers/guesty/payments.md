# Guesty: payments

This page describes how pmsbridge reads the payments recorded for a reservation in Guesty and what the AI
assistant can ask about them. Everything here is read only: no tool in this area charges, refunds or
records a payment.

## What the assistant can do

| Tool | What it answers |
|---|---|
| `list_reservation_payments` | "Has this booking been paid?" "When is the next payment due?" The payments of one reservation, each with amount and currency, status, a plain method word, the time it is due and the time it was paid. |

Statuses are shown as pending, paid, failed, refunded, partially refunded, cancelled or unknown (see the
table below).

## Card and bank details are never shown

Guesty's payments list has no way to ask for fewer fields, so Guesty sends everything about each payment
to pmsbridge, including the payment method id and, for card payments, card details. pmsbridge reads only
the fields below and drops the rest in the same step. It never reads, returns or logs: card numbers, the
last four digits, BINs, card brands, payment method ids, processor references, receipt and payout ids,
bank account details, IBANs, routing numbers, refund notes or Guesty's error texts.

The payment method is shown only as one of a short fixed list of plain words: card, cash, check or bank
transfer. Anything else, such as a card brand or a processor name, is shown as no method.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>. The page for this endpoint is in the
Payments section of the Open API reference.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| Payments of a reservation | `GET /v1/payments` | `reservationId` (required, one reservation per call) | none: Guesty answers one object with a `payments` list and totals. pmsbridge keeps at most 200 payments. |

The reservation id is checked before anything is sent. The call uses Guesty's shared rate limits, stops
after 25 seconds, and is repeated safely when Guesty is briefly unavailable.

## Fields read

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `_id` | A payment without an id is skipped. |
| reservation | the reservation id that was asked for | |
| amount and currency | `amount`, `currency` | Guesty gives amounts as numbers in the reservation's currency. If a payment has no currency, the currency of the whole answer is used. A payment with no usable amount or currency is skipped. |
| status | `status` | See below. |
| due | `shouldBePaidAt` | |
| paid | `paidAt` | |
| method | `paymentMethod.method` | Only if present and only as a word from the fixed list above. |
| refunds | `refunds[].amount`, `refunds[].refundedAt` | Used only to tell refunded from partially refunded. |

### Statuses

| Guesty status | Shown as |
|---|---|
| `PENDING`, `PENDING_AUTH`, `PROCESSING` | pending |
| `SUCCEEDED` | paid, or refunded or partially refunded when refunds that were carried out (they have a refund time) cover all or part of the amount |
| `FAILED`, `ERROR` | failed |
| `CANCELLED` | cancelled |
| `AUTHORIZATION_HOLD_SUCCEEDED`, `SECURITY_DEPOSIT_SUCCEEDED`, anything new | unknown, with Guesty's own status word shown as the extra value `pms_status` |

Holds and security deposits are shown as unknown on purpose: money is held on the card, not taken as a
payment for the stay.

## What assistants can see

An AI assistant you have approved can read the amounts, statuses and times of every reservation's
payments for the connections you allowed. Guest names are not part of this answer.

## Known gaps

- **Payment method**: Guesty's payment object documents only a payment method id, not a method word, so
  the method is usually empty. pmsbridge shows one only if Guesty includes `paymentMethod.method`.
- **Amount units**: Guesty's reference does not say whether amounts are in major or minor units. Its
  examples (for example `20.45`) suggest major units, and pmsbridge reads them that way.
- **Recorded and processor payments** are not told apart.
- **Folio and balance**: totals such as the balance due or the host payout are in Guesty's answer but are
  not shown by this tool, which lists the payments only.
- **No single payment lookup**: `GET /payments/{paymentId}` is not used.
