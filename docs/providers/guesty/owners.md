# Guesty: owners

This page describes how pmsbridge reads the property owners in Guesty and what the AI assistant can ask
about them. Everything here is read only: no tool in this area changes anything in Guesty.

## What the assistant can do

| Tool | What it answers |
|---|---|
| `list_owners` | "Who are our owners?" A page of owners with name, email, phone and the ids of their listings. |
| `get_owner` | "How do I reach the owner of this property?" One owner by id. |

Owners never include payout methods, bank details or tax details. Those are not requested from Guesty and
are not read even if Guesty were to send them.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>. The pages for these endpoints are in
the Owners section of the Open API reference.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| List owners | `GET /v1/owners` | `fields` (see below), `limit` (1 to 100), `skip`, `sort=_id`, `pagination=true` | `skip` and `limit`. With `pagination=true` Guesty answers `{skip, limit, count, totalCount, sort, results}`; pmsbridge turns the next `skip` into an opaque cursor and passes `totalCount` on as the total. A plain list (no envelope) is also understood: a full page then means there may be more. |
| Get one owner | `GET /v1/owners/{ownerId}` | `fields` (see below) | none |

Every id is percent-encoded before it goes into a path. Each call uses Guesty's shared rate limits, each
call stops after 25 seconds, and read calls are repeated safely when Guesty is briefly unavailable.

## Fields read

Both calls ask Guesty only for these fields, so nothing else is sent to pmsbridge:

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `_id` | An owner without an id is skipped. |
| full name | `fullName` | If missing, the first and last name are joined. An owner with no name is shown as "Unknown". |
| email | `email` | |
| phone | `phone` | Shown as Guesty stores it. |
| listing ids | `listings` | The ids of the owner's listings, without duplicates, at most 200. |

## What is never read

pmsbridge never asks for, reads, shows or logs: payout methods, bank accounts, IBANs, routing numbers,
tax ids, postal addresses, working capital, owner portal settings or the Guesty account id. Owners' names,
emails and phones are never written to the log.

## What assistants can see

An AI assistant you have approved can read every owner's name, email, phone and listing ids for the
connections you allowed. If that is too much for some assistants, do not approve them for that connection.

## Known gaps

- **`fields` format**: Guesty's reference describes `fields` as a list of text values but says the values
  are separated by spaces. pmsbridge sends one space-separated value, which works with either reading.
  Whatever Guesty sends back, only the fields above are used.
- **`listings` shape**: Guesty's reference gives the type as an object but describes it as an array of
  listing ids. pmsbridge reads it only when it is a list (of ids, or of objects with an `_id`); any other
  shape shows no listings.
- **Page size**: Guesty does not state a maximum for `limit`; pmsbridge asks for at most 100.
- **Inactive owners**: owners that Guesty marks as deleted or inactive are listed like any other, because
  the shared owner model has no field for it.
- **Ownerships**: the link between an owner and a listing is read from the owner's `listings`. Guesty's
  separate ownership endpoints are not used.
