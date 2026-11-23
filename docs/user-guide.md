# User guide

This guide is for the people who use pmsbridge every day: it explains how to connect your property
management system and what the connection screens mean. Setting up people, sign-in and the server itself
is covered in the [administrator guide](admin-guide.md).

## Connections

A connection links pmsbridge to your Guesty account. Everyone who is signed in can open
**Connections** (`/connections`) and see what is connected. Only administrators can add, change or remove
connections.

For each connection the list shows:

| Column | Meaning |
|---|---|
| Name | The name you gave it, and the last four characters of its API key so you can tell keys apart. |
| System | Guesty. |
| Status | **Working**, **Token budget low**, **Needs attention** or **Turned off** (see [Guesty](providers/guesty/index.md#connection-status)). |
| Changes | **Off (read only)** or **Allowed**. See [Allow changes](#allow-changes). |
| Tokens left today | How many of Guesty's 5 daily access tokens the key still has. |
| Last successful call | When pmsbridge last got a good answer from Guesty. |

## Connect Guesty, step by step

You need a Guesty user who may manage integrations, and an administrator account in pmsbridge.

1. **Create an API key just for pmsbridge in Guesty.** Open Guesty's API settings and create a new key.
   Name it something like "pmsbridge". Guesty's own guide:
   <https://open-api-docs.guesty.com/reference/authentication-2>.
2. **Do not reuse a key that another tool already uses.** Guesty lets each key get only 5 new access tokens
   every 24 hours. Every tool that shares a key shares that limit, so a shared key can run out and lock
   one of the tools out for a while. Guesty also warns against sharing keys between integrations: if you
   ever need to cut one tool off, you would have to replace the key for all of them. Never give the key to
   anyone or any tool you do not trust, especially a tool that can change prices and availability. pmsbridge
   keeps the key only on your own server, encrypted in its database.
3. **Copy the client ID and the client secret right away.** Guesty shows the secret only once. If you lose
   it, create a new key.
4. In pmsbridge, open **Connections** and select **Add a Guesty connection**.
5. Type a name (for example "Beach houses"), the client ID and the client secret. The secret is typed into
   a hidden field, is stored encrypted and is never shown again, on any page or in any log.
6. Select **Check and add**. pmsbridge asks you to confirm your password and a two-factor code (you are not
   asked again for 10 minutes), then type the secret once more if needed.

pmsbridge checks the key with Guesty straight away. **This uses one of the key's 5 daily tokens.** The
token is kept and used afterwards, so nothing is wasted. If the key or the name is already in use,
pmsbridge says so before asking Guesty for anything. A second key of a Guesty account that is already
connected is noticed only after Guesty has issued its token, so that one token is used and the connection
is not added.

A new connection is **read only**: AI assistants can look things up but cannot change anything.

## Choose what each AI assistant may use

When someone connects an AI assistant, the approval page asks which connections it may use. People can
change that later under **Connected apps**. Connections that are turned off or need attention are not offered.

## Allow changes

By default an AI assistant can only read from Guesty. To let it make changes, an administrator opens the
connection's **Allow changes** page. Read the warning: **the AI assistant will be able to change data in
your Guesty account: prices, availability, guest messages and similar. You are responsible for what it
does.** After you confirm your password and a code, the connection shows **Allowed**.

The switch is per connection. Select **Stop changes** at any time to go back to read only.

## Check, replace, turn off, remove

- **Test** sends one read-only request to Guesty with the token pmsbridge already has. It never asks Guesty
  for a new token, so it does not use up any of the 5 daily tokens. If there is no usable token yet, it says
  a new one will be fetched the next time the connection is used.
- **Replace credentials** is for a new client secret (or a new key of the same Guesty account). Enter the
  new client ID and secret. pmsbridge checks them with Guesty (one daily token) and only then swaps them.
  If they are wrong, the old ones keep working. The connection keeps its identity, so AI assistants that
  were approved keep working without being connected again. Use it also to bring back a connection that
  **needs attention**. Replacements are limited to a few a day for each connection.
- **Turn off** and **Turn on** pause and resume a connection without deleting anything.
- **Remove** deletes the connection with its stored credentials and token, and takes it away from every
  AI assistant's approval. Guesty itself is not touched, so delete the key in Guesty too if you no longer
  need it.

## From the command line

Administrators can do the same on the server. The secret is typed into a hidden prompt and is never put
on the command line. For example, to add a connection (you are asked for the secret):

```bash
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections add --label "Beach houses" --client-id <client id>
```

To feed the secret from a file or a password manager instead, use `--from-stdin` and turn off the terminal
with `-T`:

```bash
cat secret.txt | docker compose -f deploy/compose.yaml exec -T app python -m pmsbridge.admin connections add --label "Beach houses" --client-id <client id> --from-stdin
```

If you saved the secret in a file for this, delete the file afterwards (`shred -u secret.txt`). Piping it
straight from your password manager's command-line tool avoids the file altogether.

The other actions use `--connection <name or id>`:

```bash
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections list
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections rotate --connection "Beach houses" --client-id <client id>
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections test --connection "Beach houses"
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections disable --connection "Beach houses"
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections enable --connection "Beach houses"
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections stop-changes --connection "Beach houses"
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections allow-changes --connection "Beach houses" --yes
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin connections remove --connection "Beach houses" --yes
```

`allow-changes` and `remove` refuse, with exit code 2, unless you add `--yes`. `enable` works only for a
connection that was turned off: one that **needs attention** is brought back by `rotate` (or **Replace
credentials** on the page). Every action is written to the audit log with the connection's identifier
and never with a secret.

Replacing credentials from the command line while the application is running is safe: a token that was
requested with the old key is never stored over the new key's.

**What exists today.** The shorter `sudo pmsbridge connections ...` commands arrive with the installer and
command-line tool and are not available yet. The `python -m pmsbridge.admin` entry point used above also
arrives with that packaging work, so until then the commands above do not run on a fresh image, even
though the actions themselves are built and tested.
