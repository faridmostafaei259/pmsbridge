# Guesty: calendar and prices

This page describes how pmsbridge reads your calendar and price settings (Guesty calls the day-by-day
calendar "availability and pricing") and how it changes them. Reading is always allowed for an approved
assistant. Changing the calendar is the first thing pmsbridge does that **changes data in Guesty**, so it
needs an administrator to allow changes on the connection first (see [Allow changes](../../user-guide.md#allow-changes)).

## What the assistant can do

| Tool | What it answers |
|---|---|
| `get_calendar` | "Is the beach house free next weekend, and at what price?" Day by day: available or not, price per night, minimum stay, the reservation on a booked day. Up to 366 days at a time. |
| `get_pricing_settings` | "What are the standing prices of the beach house?" Currency, base and weekend price, cleaning fee, extra guest fee and guests included, security deposit, weekly and monthly discount, minimum and maximum stay. |
| `update_calendar` | "Set 180 a night for 20 to 23 December." Changes the price, the minimum stay or the availability for date ranges (up to 31 ranges in one request). **Changes data.** |

Changing the standing price settings (base price, fees, discounts) is not available. Only the day-by-day
calendar can be changed.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>. The pages for these endpoints are in
the availability and pricing (calendar) section and the listings section of the Open API reference. pmsbridge
uses the paths exactly as Guesty lists them, under `https://open-api.guesty.com/v1`.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| Read the calendar | `GET /v1/availability-pricing/api/calendar/listings` | `listingIds` (exactly one listing), `startDate`, `endDate` (both `YYYY-MM-DD`, both days included) | none; Guesty returns every day in the range. Guesty does not state a maximum range, so pmsbridge asks for at most 366 days. |
| Change the calendar | `PUT /v1/availability-pricing/api/calendar/listings/{id}` | body: `startDate`, `endDate`, and only the fields being changed: `price` (a number in the listing's currency), `minNights`, `status` (`available` or `unavailable`; `blockReason` is `Other` when blocking) | none; Guesty answers `"ok"` |
| Listing prices (for checks before a price change) | `GET /v1/listings/{id}` | `fields=_id prices` | none |
| Price settings | `GET /v1/listings/{id}` | `fields=_id prices terms` (the price settings and stay limits only) | none |

Every id is percent-encoded before it goes into a path, and the id of a listing must be a single value (an
id with a comma is refused, because Guesty reads a comma as a list). Each call uses Guesty's shared rate
limits and stops after 25 seconds. Reads are repeated safely when Guesty is briefly unavailable.

## How availability is worked out

A day is **available** when Guesty gives a number of free units (`allotment`) above zero. When there is no
such number, a day is available when its `status` is `available`. A day that has neither is shown as not
available: pmsbridge never says a day is free unless Guesty says so.

A day with a `reservationId` (or the status `booked` or `reserved`) is shown with its reservation id. A day
that is not available and not booked, and that Guesty marks with at least one block, is shown with the reason
"blocked". Guesty does not say what its block keys mean, so pmsbridge does not try to name them. The notes
staff type on a block are never read or shown.

Guesty's reference does not make clear whether the answer is a plain list of days or an object around the
list, so both are accepted. Days outside the range asked for, days of another listing and days without a
date are left out, and no day is ever invented for a date Guesty did not return. The price is shown in the
currency Guesty gives for the day (or for the whole answer).

## How a change is made

Before anything is sent, pmsbridge checks the request and refuses the whole of it, sending nothing, when:

1. **A range starts before yesterday.** Days in the past cannot be changed. Yesterday is still allowed,
   because a listing's time zone can be ahead of the server's.
2. **The calendar cannot be checked completely.** pmsbridge reads the calendar for all the dates in the
   request (one read). Every day of every range must be in the answer, and for a price change every one of
   those days must also have a price in the listing's currency. Otherwise the range is refused with a plain
   reason, because the current prices and bookings could not be checked.
3. **A price is in another currency** than the listing's. The assistant must give the currency with every
   price. The listing's currency is the one on the listing record in Guesty; if the record has none, a price
   change is refused.
4. **A price looks like a mistake.** The price must be at least a quarter and at most four times the
   listing's own **base price** (from the listing record; a listing with no base price cannot have its
   prices changed through pmsbridge). It is also compared with each day's own current price: it may not take
   any day below a quarter of its current price or above four times it. The base-price limit stops a
   series of changes from moving a price far away one allowed step at a time. The message asks the person to
   change the price in Guesty directly if it is intended.
5. **Availability would change on a day that has a reservation**, whether opening or blocking. A date that
   Guesty lists more than once counts as booked if any entry for it is booked, and so does a day that carries
   a reservation object. Prices and minimum stays on a booked day can be changed.
6. **Availability would change on a multi-unit day** (a day for which Guesty gives a unit count), because
   the change would not be reliable. Prices and minimum stays on those days can be changed.

Opening days also lifts owner, maintenance and other blocks on them: only days with a reservation are
protected.

The listing record is only read when the request sets a price.

Then each range is sent as its own `PUT`, in order, with only the fields that were set. Blocking a range
sends the reason "Other". Before each `PUT`, pmsbridge checks how much of the 25 seconds is left; if less
than two seconds remain, it stops and says which ranges were applied and that the others were not sent.
When every range was accepted, pmsbridge reads the changed dates once more and returns those days, so the
assistant (and you) can see the result. If that last read fails, the message says "The changes were
applied but the updated calendar could not be read; check it in Guesty."

The ranges in one request may not span more than 366 days from the first date to the last.

### When a range fails part-way

A change is never sent again automatically. The only exception is when Guesty answers "too many requests"
with a wait time: it did not process the request, so pmsbridge sends it again after that wait. A server
error, a timeout or a dropped connection on a change is **not** repeated.

If range 2 of 3 fails, the message names exactly what happened, for example:

> Range 2 of 3 (2030-12-24 to 2030-12-25) did not complete and may or may not have been applied. Range 1
> was applied. Range 3 was not sent. Check the calendar before trying again: the outcome of range 2 is
> unknown.

When Guesty clearly refused a range (for example it rejected the request as invalid), the message says it
was not applied. When the first range fails and it is clear it was not applied, the message says nothing was
changed. Messages never contain text from Guesty.

## Price settings

`get_pricing_settings` asks Guesty for the listing with `fields=_id prices terms`, so only the price
settings and the stay limits come back (nothing about channels, descriptions or owners), and reads these
values:

| Shown as | Guesty field | Notes |
|---|---|---|
| currency | `prices.currency` | Required. Without it the settings are refused. |
| base price, weekend price | `prices.basePrice`, `prices.weekendBasePrice` | Per night. Guesty uses the base price for weekends when there is no weekend price. |
| cleaning fee | `prices.cleaningFee` | Only when it is a plain number. Guesty can also hold fees per channel in other places; those are not shown. |
| extra guest fee, guests included | `prices.extraPersonFee`, `prices.guestsIncludedInRegularFee` | Per extra guest, and how many guests the base price covers. |
| security deposit | `prices.securityDepositFee` | |
| weekly and monthly discount | `prices.weeklyPriceFactor`, `prices.monthlyPriceFactor` | Guesty stores a factor (0.8 means 20 percent off). Shown as a percent: (1 - factor) x 100, rounded to two decimals, only for a factor above 0 and up to 1. |
| minimum and maximum stay | `terms.minNights`, `terms.maxNights` | |

A value that is not a plain non-negative number is left out. Other values in the `prices` and `terms`
objects (cancellation terms, the price in US dollars) are not shown, and the rest of the listing is not
requested.

## What assistants can see

An approved assistant can read the prices and availability of the connections you allowed, and the
reservation id on booked days (it cannot see the guest from the calendar). It can change the calendar only
where an administrator allowed changes on the connection, and each approval is limited to a few changes per
minute.

## Known gaps

- **Prices far from the base price**: a price must stay between a quarter and four times the listing's base price,
  and within a quarter and four times each day's current price. Days already priced above four times the base
  price (for example peak dates) cannot have their price changed through pmsbridge, and neither can a range whose
  current day prices differ by more than sixteen times. Split the range or change those prices in Guesty.
- **Several units (multi-unit listings)**: for a listing with units, Guesty gives a count of free units and
  pmsbridge shows the day as available when at least one is free. The number is kept as the extra value
  `allotment`. Availability changes on such days are refused; prices and minimum stays can be changed, and
  are sent to the listing as a whole.
- **Block types**: Guesty does not list what its block keys mean, so a blocked day is only shown as
  "blocked". Blocking through pmsbridge always uses the reason "Other". Guesty's reference does not say what
  the reason does for a day that is opened again.
- **Maximum range**: Guesty does not state how many days a read or a change may cover. pmsbridge keeps to
  366 days; if Guesty refuses a range, the assistant gets a plain message.
- **Answer shape**: it is not certain whether Guesty wraps the list of days in an object. Both shapes are
  accepted.
- **Prices and taxes**: Guesty does not say whether calendar prices include taxes or fees. They are shown as
  Guesty gives them.
- **Several changes at once**: Guesty has an endpoint that changes several listings in one request. It is not
  used: each range is its own request so a failure can be reported exactly.
