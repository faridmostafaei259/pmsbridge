# Guesty: reservations

This page describes how pmsbridge reads your bookings (Guesty calls them reservations) and what the AI
assistant can ask about them. Everything here is read only: no tool in this area changes anything in
Guesty.

## What the assistant can do

| Tool | What it answers |
|---|---|
| `list_reservations` | "Who checks in tomorrow?" A page of reservations, earliest check-in first (set `check_in_from` to leave out past bookings), filtered by property, status, check-in date, check-out date, booking date or confirmation code. |
| `get_reservation` | "Show booking ABC123." "How much does the guest still owe?" One reservation with its notes, the guest's name, email and phone, and the balance still due. |

The list leaves out the guest's contact details and the money, because Guesty does not include them in
its search. `get_reservation` adds them when Guesty provides them.

Adding a note to a reservation is not available yet. It will arrive together with the other tools that
change data.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>. The pages for these endpoints are
the reservations, guests and reservation reports sections of the Open API reference.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| List reservations | `GET /v1/reservations-v3/search` | `filter[listingId]`, `filter[status]` (a comma list), `filter[confirmationCode]`, `filter[checkIn][gte]` and `[lte]`, `filter[checkOut][gte]` and `[lte]` (dates as `YYYY-MM-DD`, compared with the listing's local date), `filter[createdAt][gte]` and `[lt]` (ISO 8601 times), `sort=checkIn`, `limit` (1 to 100), `skip` | `skip` and `limit`; Guesty says whether there is more (`pagination.hasMore`) and gives no total. pmsbridge turns the next `skip` into an opaque cursor. |
| Get one reservation | `GET /v1/reservations-v3` | `reservationIds` with exactly one id | none. Guesty has no "get by id" path, so this call takes a list of up to ten ids and pmsbridge sends one. |
| Guest contact (best effort) | `GET /v1/guests-crud/{guestId}` | `fields=fullName email phone` | none |
| Balance (best effort) | `GET /v1/reservations-reports/reservations/search` | `filter[confirmationCode]`, `limit=10` | none needed |

Every id is percent-encoded before it goes into a path, and the id of a reservation must be a single
value (an id with a comma is refused). Each call uses Guesty's shared rate limits, each call stops after
25 seconds, and read calls are repeated safely when Guesty is briefly unavailable.

### How `get_reservation` works

1. pmsbridge asks Guesty for the reservation by id. An empty answer or a "not found" answer becomes a
   plain "Reservation not found" message.
2. Then, for the guest's name, email and phone, it reads the guest record named on the reservation.
3. Then, for the balance, it reads the reservations report row with the same confirmation code and takes
   the balance due and its currency.

Steps 2 and 3 are best effort. If either one fails (Guesty answers with an error, the guest or the report
row cannot be found, or the answer cannot be understood), the reservation is still returned and only that
part is left empty. A failure is logged with the reservation id and the kind of error, never with any
guest detail.

The whole `get_reservation` request, including the guest and balance look-ups, stops after 25 seconds. If
time runs out after the reservation itself was read, the reservation is still returned and only the guest
details or the balance stay empty.

### How paging works

The assistant gets a `next_cursor` when Guesty says there is more and sends it back as `cursor`. The
cursor only records how many reservations to skip. pmsbridge refuses a cursor it did not create.

### Filters

| Assistant asks for | Sent to Guesty |
|---|---|
| a property | `filter[listingId]` (Guesty matches the first stay of the reservation) |
| statuses | `filter[status]`, using the status map below (for example "pending" becomes `awaiting_payment,reserved`) |
| check-in from and to | `filter[checkIn][gte]` and `[lte]`, inclusive |
| check-out from and to | `filter[checkOut][gte]` and `[lte]`, inclusive |
| booked from and to | `filter[createdAt][gte]` at the start of the first day and `[lt]` at the start of the day after the last day, so the last day counts in full (UTC) |
| search text | `filter[confirmationCode]` (exact match) |

## Statuses

| Guesty status | Shown as |
|---|---|
| `inquiry` | inquiry |
| `awaiting_payment`, `reserved` | pending |
| `confirmed` | confirmed |
| `checked_in` | checked in |
| `checked_out`, `completed` | checked out |
| `canceled` | cancelled |
| `declined` | declined |
| `expired` | expired |
| `closed`, `alteration`, anything else | unknown |

The status Guesty sent is always kept as the extra value `pms_status`, so an "unknown" reservation can
still be told apart.

## What is shown and how it is mapped

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `reservationId` (list) or `_id` (get) | An item without an id is skipped. |
| listing id | `stay[0].listingId`, or `stay[0].unitId` or `unitTypeId` on a get | An item without one is skipped. |
| status | `status` | See the table above. |
| channel | `channel` (list) or `platform` (get) | For example `airbnb2`, `bookingCom`, `direct`. |
| confirmation code | `confirmationCode` | |
| guest id | `guestId` (list) or `bookerId` (get) | |
| guest name, email, phone | `fullName`, `email`, `phone` of the guest record | `get_reservation` only. A guest with no name is shown as "Unknown". |
| check-in and check-out | `checkInDateLocalized`, `checkOutDateLocalized` | Dates in the listing's own time zone. When missing, the earliest check-in and the latest check-out of the stays are used. |
| nights | worked out from the two dates | Guesty has no nights field on these calls. |
| guests | `stay[].guestsCount` | The largest party over the stays. |
| balance due | `money.balanceDue` with `money.currency` of the report row | `get_reservation` only. |
| booked at | `createdAt` | |
| notes | `notes.guest`, `notes.specialRequests`, `notes.other`, `notes.cleaning` | Shown as "Guest note:", "Special requests:", "Staff note:" (Guesty's `other`) and "Cleaning note:". |
| extra values | `status` as `pms_status`, `source`, `guestStay.status` as `guest_stay_status` | Plain display values only. |

An item that cannot be read (no id, no listing, no usable dates, or check-out before check-in) is
skipped, and only its id is written to the log, never its content.

### Notes and door codes

Guesty's `notes` object also holds `keyCode` (a door or lock code) and `doneBy` (who wrote the note).
pmsbridge never reads either: they are dropped before anything is built, so they cannot appear in an
answer, an extra value or a log line. Guesty's search and get calls do not let pmsbridge leave them out
of the response, so they do reach pmsbridge's memory for a moment and are then discarded.

Things pmsbridge never reads or shows: door and lock codes, payment methods, cards, payouts, bank
details, the list of payments in the report, channel details (`channelMetadata`, `integrationId`),
virtual-card flags. pmsbridge never copies any nested object into an answer.

## What assistants can see

An AI assistant you have approved can read everything in this area for the connections you allowed. That
includes guests' names, email addresses and phone numbers (on a single reservation) and anything staff or
guests wrote in the notes. If you do not want an assistant to see door codes, keep them out of the
notes: pmsbridge drops Guesty's key-code field, but a code typed into a normal note is shown.

## Known gaps

- **Search by guest name**: Guesty's reservation search cannot find a guest by name, email or phone, so
  the search text only matches a confirmation code, exactly. To find a guest's bookings, list by date and
  look at the guest on each one with `get_reservation`.
- **Total price**: Guesty has no single total-price field on these calls (the report shows a subtotal
  and taxes separately and the reference is unclear how they combine), so the total price is left empty.
  The balance due is shown instead.
- **Several units in one booking**: for a booking that spans several listings or units, pmsbridge shows
  the first one (`stay[0]`), the first check-in to the last check-out, and the largest party. Filtering
  by property matches the first stay only.
- **How a list of ids is sent**: Guesty's reference does not say whether several ids are sent as a
  repeated parameter or a comma list. pmsbridge only ever sends one id, so it does not matter.
- **Filter operators**: the reference lists the filter operators but not the exact form for an exact
  match, so pmsbridge sends the plain parameter (`filter[status]=...`) for equality and lists.
- **Balance lookup**: the balance comes from the reservation reports, which Guesty fills by a report
  sync. A very recent booking may not have a balance yet (it is then left empty), and a payment made
  recently may not be reflected yet; check Guesty before acting on the amount.
- **Phone format**: Guesty's reference does not state the format of phone numbers, so they are shown
  as Guesty stores them.
- **Old reservation endpoints**: Guesty's older reservation endpoints are not used.
