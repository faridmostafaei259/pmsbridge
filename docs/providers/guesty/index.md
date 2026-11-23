# Guesty

pmsbridge connects to Guesty with a Guesty Open API key (a client ID and a client secret). This page
explains how the connection behaves and what to watch for. For the pages that add and manage
connections, see the [user guide](../../user-guide.md).

## Get a client ID and secret

Guesty may rename its menus, so follow Guesty's own guide if the names below differ:
<https://open-api-docs.guesty.com/reference/authentication-2>.

1. Sign in to Guesty as a user who may manage integrations.
2. Open the API settings and create a new API key. Guesty gives you a client ID and a client secret.
3. Copy both values straight away. Guesty shows the client secret only once.
4. Keep them somewhere safe until you add them to pmsbridge.

Create **one dedicated key for pmsbridge** and do not share it with other tools. Guesty's limit of new
tokens (below) is counted per key, so a key shared with another tool runs out sooner.

Do not share the key with anyone you do not trust: a key can read and, if you turn write actions on,
change prices, availability and messages. pmsbridge keeps the key on your own server, encrypted in
its database, and write actions stay off until an administrator turns them on.

## The token limit

Guesty gives each API key at most **5 new access tokens every 24 hours**, and every other tool that
uses the same key shares that limit. A token lasts 24 hours. If the limit is used up, pmsbridge cannot
get a new token until Guesty releases the next one. Requests keep working for as long as the token it
already holds is valid; only getting a new token is blocked until Guesty's reset time.

pmsbridge therefore asks for as few tokens as possible:

- It asks for one token per connection per day and keeps it, encrypted, in its database. Restarting or
  upgrading pmsbridge does not request a new one.
- It renews the token shortly before it expires (45 minutes before by default; change it with
  `GUESTY_TOKEN_REFRESH_MINUTES`, 30 to 60).
- Only one renewal runs at a time, even when many requests arrive together.
- It never repeats a failed token request straight away. After any failed attempt it pauses new
  attempts for one minute, then two, and so on up to 15 minutes, and a success ends the pause. During
  a pause, a token that is still valid keeps being used.
- It records how many tokens are left. With one or none left, the connection shows the status
  **Token budget low** and pmsbridge stops renewing early.
- Checking a new connection, or new credentials for an existing one, uses one token. pmsbridge keeps that
  token instead of discarding it, and refuses a key that is already connected before it asks for one.
  While credentials are being replaced, no other renewal for that connection runs.
- The **Test** button and `connections test` never ask for a token. They use the stored one for a single
  read-only request. If a token was issued but could not be saved to the database, pmsbridge keeps it in
  memory, uses it and saves it again before asking for another.
- Replacing credentials from the command line while the application runs is safe: before a token is
  stored, pmsbridge checks that it was issued for the key now on record, and a token issued for the old
  key is discarded instead of being stored over the new one.
- A token request that has not finished after 30 seconds counts as failed and starts the pause above.

## Connection status

| Status | Meaning |
|---|---|
| Working | Everything is fine. |
| Token budget low | One token or fewer left today. The connection keeps working with the token it has. The dashboard shows when the next token frees up. |
| Needs attention | pmsbridge stopped using the connection until an administrator checks it. Causes: Guesty did not accept the client ID and secret; Guesty rejected requests even after the token was renewed; Guesty issued a token for a different key or a different Guesty account than the one stored; the Guesty account is already connected under another connection. |
| Turned off | Turned off by an administrator. It can be turned on again; a connection that needs attention cannot be switched on, only fixed by replacing its credentials. |

The same Guesty account and the same key cannot be added twice (pmsbridge refuses a key that another
connection already uses before it asks Guesty for anything), and a connection's account never changes:
if its key is replaced by one from a different account, the connection stops and asks for attention.
Replacing a connection's credentials with ones that Guesty accepts puts it back to **Working**.

## Request limits

Guesty allows 15 requests per second, 120 per minute and 5,000 per hour for each account. pmsbridge
keeps below those limits and runs at most 8 requests at a time for an account (change with
`PMS_MAX_CONCURRENCY`, 1 to 15). When Guesty says to wait, pmsbridge waits only as long as the AI
assistant is willing to wait; otherwise it returns a clear message with the number of seconds to try
again in.

Reads are repeated a few times after a temporary problem. Changes are never repeated automatically,
with one exception: when Guesty answers "too many requests" with a wait time, it did not process the
request, so pmsbridge sends it again after that wait. If a change fails part-way, the message says
its outcome is unknown, so you can check in Guesty before trying again.

After an expired token, pmsbridge gets a new token and repeats reads once. It does not repeat
changes: the AI assistant is told that nothing was changed and that it can try again.

## Endpoints used

Each feature has its own page with the Guesty endpoints it calls and how their data is used:

- [Listings, listing details and photos](listings.md)
- [Reservations](reservations.md)

pmsbridge itself also calls these, for every feature:

| Purpose | Endpoint | Documentation |
|---|---|---|
| Get an access token | `POST https://open-api.guesty.com/oauth2/token` (client credentials, scope `open-api`) | <https://open-api-docs.guesty.com/reference/authentication-2> |
| Check a connection (the Test button) | `GET https://open-api.guesty.com/v1/listings?limit=1&fields=_id` with the stored token | <https://open-api-docs.guesty.com/> |
