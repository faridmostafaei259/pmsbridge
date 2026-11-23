# Guesty: guests

This page describes how pmsbridge reads your guests' records in Guesty and what the AI assistant can
ask about them. Everything here is read only: no tool in this area changes anything in Guesty.

## What the assistant can do

| Tool | What it answers |
|---|---|
| `list_guests` | "Show me the guests, page by page." A page of guests with name, email, phone and language. |
| `get_guest` | "What is this guest's email?" "Which language does this guest prefer?" One guest by id. |

The reliable way to reach the guest of a booking is `get_reservation`, which shows the guest's name,
email and phone for that booking (see [Reservations](reservations.md)). Use `get_guest` when you already
have a guest id.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>. The pages for these endpoints are
the guests (guests-crud) section of the Open API reference.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| List guests | `GET /v1/guests-crud` | `columns` (required, space separated, see below), `limit` (1 to 100), `skip` | `skip` and `limit`. Guesty answers `{results, total}`; pmsbridge turns the next `skip` into an opaque cursor and passes the total on. |
| Get one guest | `GET /v1/guests-crud/{guestId}` | `fields` (required, space separated, see below) | none |

Every id is percent-encoded before it goes into a path. Each call uses Guesty's shared rate limits, each
call stops after 25 seconds, and read calls are repeated safely when Guesty is briefly unavailable.

## Fields read

Both calls ask Guesty only for these fields, so nothing else is sent to pmsbridge:

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `_id` | A guest without an id is skipped. |
| full name | `fullName` | A guest with no name is shown as "Unknown". |
| first name, last name | `firstName`, `lastName` | |
| email | `email` | The main email. The list also accepts `guestEmail`. |
| phone | `phone` | The main phone, shown as Guesty stores it. The list also accepts `guestPhone`. |
| language | `preferredLanguage` | Guesty's reference says text but its example shows a list; pmsbridge takes the text, or the first text of a list. |
| returning guest | `returningGuest` | Shown as the extra value `returning_guest`. |

## What is never read

pmsbridge never asks for, reads, shows or logs: passport and identity numbers, birthdays, addresses,
staff attention notes, payment methods, the extra emails and phones lists, or tags. They are left out of
the request, so Guesty does not send them. Guests' names, emails and phones are never written to the log.

## What assistants can see

An AI assistant you have approved can read every guest's name, main email, main phone and language for
the connections you allowed. If that is too much for some assistants, do not approve them for that
connection.

## Known gaps

- **No text search**: Guesty cannot search guests by name or email. If an assistant asks for a search,
  pmsbridge answers with a clear message (find the guest through their reservation, or list guests page
  by page) and sends nothing to Guesty.
- **List column names**: Guesty's reference does not say which column names the list answers with.
  pmsbridge asks for the same names as the single-guest call and also understands `guestEmail`,
  `guestPhone` and `id`. If a list shows guests without an email or phone, this is the likely cause.
- **Page size**: Guesty does not state a maximum for `limit`; pmsbridge asks for at most 100.
- **Phone format**: Guesty does not state the format of phone numbers, so they are shown as stored.
