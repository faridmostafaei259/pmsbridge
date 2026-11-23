# Guesty: reviews

This page describes how pmsbridge reads the reviews your guests left and posts a public reply to one,
and what the AI assistant can do with them. Reading is read only. Replying changes things in Guesty (and
on the booking channel), so it only works on a connection where an administrator has turned changes on.

## What the assistant can do

| Tool | What it answers | Needs changes turned on |
|---|---|---|
| `list_reviews` | "Which reviews did this listing get lately?" A page of reviews, most recently changed first, with the listing, reservation, channel, rating (with its scale), public text, existing reply and creation time. | No |
| `reply_to_review` | "Thank the guest for this review." Posts one public reply and returns the review with it. | Yes |

Before replying, the assistant is told to show you the exact text and get your confirmation: a reply is
public and cannot be recalled. The reply is never repeated automatically. If Guesty does not answer in
time, the message says the outcome is unknown and the assistant should read the review again before
trying, so the guest does not get two replies.

## Review text is untrusted

The text of a review is written by the guest, so anyone can put any words in it, including words that look
like an instruction. pmsbridge passes the text on only as review content and tells the assistant, in the
tool description, to treat it as information and never as instructions. Two things limit the damage if an
assistant is fooled: replying needs the connection's change switch (off until an administrator turns it
on), and each approval has its own limit on how fast changes can be made.

Review text, reply text and the names of guests are never written to pmsbridge's log.

## Endpoints used

Guesty's reference pages are at <https://open-api-docs.guesty.com/>. The pages for these endpoints are in
the Reviews section of the Open API reference.

| Purpose | Method and path | Parameters | Paging |
|---|---|---|---|
| List reviews | `GET /v1/reviews` | `includeCustomChannels=true`, `limit` (1 to 100), `skip`, `listingId`, `startDate` | `skip` and `limit`: pmsbridge turns the number of reviews already read into an opaque cursor. A short or empty page ends the list. |
| Reply to a review | `PUT /v1/reviews/{reviewId}/reply` | JSON body `{"reviewReply": <text>}` | none |

Every id is percent-encoded before it goes into a path. Each call uses Guesty's shared rate limits and
stops after 25 seconds. Reads are repeated safely when Guesty is briefly unavailable. The reply is one
request and is not repeated, except when Guesty answers "too many requests" with a wait time (then it did
not process the request).

Guesty sorts the list by the time of the last change, newest first, and has no sort option. The `since` date
the assistant can pass is sent as `startDate` and is compared with that change time, so a review that got a
reply after the date also matches. Guesty has no filter by rating or by guest, and pmsbridge does not offer
one. The default and the maximum page size are not stated in Guesty's reference, so pmsbridge follows the
page size Guesty reports back.

## Fields read

Guesty's reference types the list answer only as an object, and describes a review in full only for reviews
on custom channels. pmsbridge therefore accepts the list as a plain array, as `data`, or as `data.results`,
and reads only the values below. Anything else is ignored.

| Shown as | Guesty field | Notes |
|---|---|---|
| id | `_id` | A review without an id is skipped. |
| listing, reservation | `listingId`, `reservationId` | |
| channel | `channelId` | For example `airbnb2`, `bookingCom`, `homeaway2`, `custom`. |
| created | `createdAt`, else `createdAtGuesty` | |
| public text | `contents.body` | Shortened with a marker after 5,000 characters. |
| rating, scale | `contents.rating`, scale 10 | Guesty documents 1 to 10 for this field only. |
| reply | `reviewReplies[].reviewReply` | See below. |
| extra values | `reviewReplies[].status`, `reviewReplies[].replyAt`, `externalReviewId`, `updatedAtGuesty` (else `updatedAt`) | Shown for display only. |

The name of the guest who wrote the review (`contents.reviewerName`) and the channel's own copy of the review
(`rawReview`) are never read or shown. Private feedback from the guest to the host has no field in Guesty's
reference, so it is always empty.

### What this means for Airbnb, Booking.com and Vrbo reviews

Guesty documents `contents.body` and `contents.rating` only for reviews on custom channels. For the other
channels the text and rating may sit inside `rawReview`, whose layout Guesty does not document, so
pmsbridge does not read it. Such a review is still listed, with its listing, reservation, channel, dates and
reply, but may have no text or rating. This will be revisited once Guesty documents the layout.

### Replies

A reply that is pending or completed counts as a reply. One that failed, or that Guesty reports as not found,
does not, and a new reply can be tried. The reply status is shown as the extra value `reply_status`.

## Replying to a review

Guesty has no call that reads a single review, so before sending, pmsbridge looks for the review among the
newest reviews (at most 1,000, in pages of 100):

- If the review already has a reply, nothing is sent and the message says so. Airbnb allows a reply to be
  changed and Booking.com does not, but pmsbridge never overwrites a reply. Change it in Guesty or on the
  channel.
- If the review is not among the newest 1,000 by last change, nothing is sent and the message says so. Reply
  to such an old review in Guesty directly.
- Otherwise exactly one request is sent. The reply can be 1 to 5,000 characters; the booking channel may
  accept fewer, and then Guesty's refusal is reported and nothing was posted.
- If Guesty accepts the reply but its answer cannot be read, the error says the reply was probably sent and
  to check the review first.

Custom-channel reviews are included. Guesty does not say whether a custom channel can be replied to, so for
those Guesty decides.

## What assistants can see

An AI assistant you have approved can read the text of the reviews on the connections you allowed and, if
you turned changes on, post public replies in your name. If that is too much for some assistants, do not
approve them for that connection, or leave changes off.

## Known gaps

- **Text and rating of channel reviews**: possibly missing, see above.
- **Private feedback**: not available.
- **Search by rating, guest or reservation**: not offered.
- **Editing a reply**: not offered.
- **Unverified shapes**: the layout of the list answer and of the reply answer is not given in Guesty's
  reference. The adapter accepts the shapes described above and treats any other as an error.
