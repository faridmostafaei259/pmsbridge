# Guesty: listings

This page describes how pmsbridge reads your properties (Guesty calls them listings) and what the AI
assistant can ask about them. Everything here is read only: no tool in this area changes anything in
Guesty.

## What the assistant can do

| Tool | What it answers |
|---|---|
| `list_listings` | "What properties do we have?" A page of listings, searchable by title, address or internal note. |
| `get_listing` | "Tell me about the beach house." Name, address, bedrooms, guest capacity, base price, time zone, check-in and check-out times. |
| `get_listing_details` | "What does the listing say?" Descriptions, amenities, check-in instructions and custom fields. |
| `list_listing_photos` | "Show me its photos." Image addresses, thumbnails and captions in display order. |

Changing photos (captions, order, deleting) is not available yet. It will arrive together with the
other tools that change data.

## Endpoints used

| Purpose | Method and path | Parameters | Paging | Documentation |
|---|---|---|---|---|
| List listings | `GET /v1/listings` | `limit` (1 to 100), `skip`, `sort=title`, `fields` (only what pmsbridge shows), `active=true` when only active listings are wanted, `q` for text search (Guesty searches the title, address and internal note) | `skip` and `limit`; pmsbridge turns the next `skip` into an opaque cursor | <https://open-api-docs.guesty.com/reference/propertieslistinglist> |
| Get one listing | `GET /v1/listings/{id}` | `fields` | none | <https://open-api-docs.guesty.com/reference/propertieslistingget> |
| Listing content | `GET /v1/listings/{id}` | `fields` (descriptions, amenities, check-in instructions, custom fields) | none | <https://open-api-docs.guesty.com/reference/propertieslistingget> |
| Listing photos | `GET /v1/properties-api/property-photos/property-photos/{id}` | none | none; Guesty returns the whole list | Guesty's reference page for property photos (<https://open-api-docs.guesty.com/>) |

Every id is percent-encoded before it goes into a path, so an id can never reach another address.
Each call uses Guesty's shared rate limits, each call stops after 25 seconds, and read calls are repeated
safely when Guesty is briefly unavailable.

### How paging works

The assistant gets a `next_cursor` when there may be more listings and sends it back as `cursor`.
The cursor only records how many listings to skip. pmsbridge refuses a cursor it did not create.
Guesty's reference is unclear whether the list answer is a plain array or an object with a `results`
list, so pmsbridge accepts both. A total count is shown only when Guesty provides one.

## What is shown and how it is mapped

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `_id` | A listing without an id is skipped. |
| title | `title` | "Unknown" if Guesty has none. |
| nickname | `nickname` | |
| status | `active` | active, inactive, or unknown. |
| property type | `propertyType` | |
| address | `address.street`, `address.city`, `address.country`, `address.lat`, `address.lng` | No state or postal code (see gaps). |
| bedrooms | `bedrooms` | |
| max guests | `accommodates` | |
| time zone | `timezone` | |
| base price | `prices.basePrice` with `prices.currency` | Left empty when the currency is not a three-letter code. |
| check-in and check-out time | `defaultCheckInTime`, `defaultCheckOutTime` | `HH:mm` in the listing's time zone. |
| tags | `tags` | |
| extra values | `roomType`, `address.full`, `isListed`, `isVirtual`, `isTest`, `terms.minNights`, `terms.maxNights`, `cleaningStatus.value` | Plain display values only. |
| descriptions | `publicDescription` | See the gaps below. Guesty's private (staff-only) description is never requested or read. |
| amenities | `amenities`, `amenitiesNotIncluded` | |
| check-in instructions | `checkInInstructions.primaryCheckIn`, `.alternativeCheckIn`, `.notes` | Written as one short text. |
| custom fields | `customFields` | Plain text, number and yes/no values only. |
| photos | `_id`, `original`, `thumbnail`, `caption`, `index` | Sorted by `index`. Only web addresses starting with http or https are shown. |

Things pmsbridge never reads or shows: the channel connections of a listing (`integrations`, which
can contain credentials), owner details, staff-only listing text (Guesty's private description) and
any nested object. A listing item that cannot be read is
skipped, and only its id is written to the log, never its content.

Listings are requested with an explicit list of fields, so Guesty is never asked for channel
connections in the first place.

## What assistants can see

An AI assistant you have approved can read everything in this area for the connections you allowed.
That includes anything written in a listing's check-in instructions and custom fields in Guesty. If
you do not want an assistant to see door codes or lock-box codes, keep them out of those fields.

## Known gaps

- **Bathrooms** are not part of Guesty's listing record, so they are left empty. Guesty keeps them in a
  separate "spaces" area that is not read yet.
- **State and postal code** are not part of the address on Guesty's listing record, so they are left
  empty.
- **Rooms, beds and house rules** are left empty. Guesty serves them from separate endpoints that are
  addressed by unit type, and the relation to a listing is not documented well enough to use.
- **Description keys**: Guesty does not document the keys inside `publicDescription`. pmsbridge reads
  every text value in it and labels the well-known keys (summary, space, access, neighborhood, transit,
  interaction, notes, house rules); other keys are shown as "other".
- **Inactive listings**: Guesty's reference says the `active` filter can be left out to get every
  listing, but its default is shown as `true`. pmsbridge only sends `active=true` when asked for active
  listings, and otherwise leaves it out. If your account only ever shows active properties, this is why.
  Guesty also shows `pmsActive` and `listed` as defaulting to `true`, so listings that are not in the
  PMS feature or not listed on channels may be hidden in the same way. pmsbridge does not send either.
- **Custom field names**: Guesty does not document the shape of `customFields`, so values are shown under
  the keys Guesty uses, which may be field ids.
- **Photo count and sizes**: the photo list gives only the full-size and thumbnail addresses. At most
  200 photos are shown for a listing.
