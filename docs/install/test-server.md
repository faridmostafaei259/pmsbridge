# Try pmsbridge on a test server (install by hand)

This page shows how to install pmsbridge by hand on a Linux server, step by step, so that you can try it
with a real domain and a real AI assistant.

**Most people should use the installer instead.** One command does everything on this page for you, and it
asks only for your domain name and your email address: see [Install pmsbridge](index.md). Use the manual
steps here if you cannot run the installer, or if you want to see what it does. They give the same result.

The `pmsbridge` command-line tool is not released yet. Until it is, you start and look after the
installation with the `docker compose` commands on this page. Nothing here is backed up automatically (see
[Where your data lives](#where-your-data-lives)).

## What you need

- A Linux server with Docker and the Docker Compose plugin installed. Check with `docker --version` and
  `docker compose version`. If your account is not allowed to run Docker, put `sudo` in front of every
  `docker` command on this page.
- A domain name, such as `mcp.example.com`, with a DNS `A` record that points at the server's public
  address. Check from any computer with
  `nslookup mcp.example.com`.
- Ports 80 and 443 open to the internet, in the server's firewall and in any cloud firewall. Port 80 is
  needed to get the certificate.
- Git, to download the code.
- An email address for the certificate authority, which uses it to contact you about your certificate.
- A Guesty API key (a client ID and a client secret) created just for pmsbridge. The
  [user guide](../user-guide.md#connect-guesty-step-by-step) explains how.

AI assistants only accept an address that has a real certificate, which is why a real domain is needed.
[TLS and domains](../tls-and-domains.md) explains why.

## 1. Download pmsbridge

```
git clone https://github.com/faridmostafaei259/pmsbridge.git
cd pmsbridge
```

Run every command below from this folder, the one that contains `deploy/`.

## 2. Write your settings

Copy the example settings file and open the copy in an editor:

```
cp deploy/env.example deploy/.env
nano deploy/.env
```

Change `DOMAIN` and `PUBLIC_BASE_URL` to your domain (the same host name in both, and
`PUBLIC_BASE_URL` starts with `https://`), and `ACME_EMAIL` to your email address. Leave
`ENVIRONMENT=production` and `TLS_MODE=auto` as they are. Every setting is explained in the
[configuration reference](../configuration.md).

Then create the encryption key. It protects the Guesty credentials that pmsbridge stores. This command adds
one line to `deploy/.env`:

```
(umask 077; printf 'ENCRYPTION_KEYS=k1:%s\n' "$(openssl rand -base64 32)" >> deploy/.env)
```

Copy the new `ENCRYPTION_KEYS=` line into a password manager. If you lose the key, the stored credentials
cannot be recovered and you would have to enter them again. Never share it.

Finally, make the file readable only by you:

```
chmod 600 deploy/.env
```

## 3. Start it

```
docker compose -f deploy/compose.yaml up -d --build --wait
```

The first start builds the images, which takes a few minutes. The command finishes when both services
(`app` and `caddy`) are healthy. Caddy asks for your certificate on its own; if your DNS record has only
just been created it keeps trying until the record reaches the server.

Check that it works by opening `https://<your-domain>/healthz` in a browser, or from a computer:

```
curl https://mcp.example.com/healthz
```

You should see `{"status":"ok"}`. If you do not, see [If something does not work](#if-something-does-not-work).

## 4. Create the first administrator

Nobody can sign in yet. Create the first administrator from inside the running application, using your own
email address:

```
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users create-admin --email you@example.com
```

The command prints a one-time link that looks like `https://mcp.example.com/setup?token=...`. A line of
JSON that starts with `{"time":` may appear above it: that is the record of what you did (it contains no
link and no email address), and you can ignore it. Open the link in your browser within 24 hours, then:

1. Choose a password (12 to 256 characters).
2. Set up two-factor sign-in. Scan the picture with an authenticator app (or type the text key into it)
   and enter the 6-digit code. Administrators must use two-factor sign-in.
3. Save the recovery codes. They are shown only once.
4. Sign in with your email address, your password and a code from the app.

The [administrator guide](../admin-guide.md#first-sign-in-the-setup-link) has more about this step, including
what to do if you lose the link, the password or your phone.

## 5. Add your Guesty connection

In pmsbridge, open **Connections**, select **Add a Guesty connection**, and enter a name, the client ID and
the client secret. The [user guide](../user-guide.md#connect-guesty-step-by-step) goes through it step by
step. Adding the connection uses one of the key's five daily Guesty tokens.

A new connection is read only: AI assistants can look things up but cannot change anything.

## 6. Connect an AI assistant

Add `https://<your-domain>/mcp` as a custom connector in your AI assistant and approve it when pmsbridge
asks. The steps are in [Connect an AI assistant](../connect-ai-clients.md).

## What works today

pmsbridge works with Guesty only. Hostaway support comes in a later release.

Assistants can read listings, reservations, calendars and prices, guests, conversations, reviews, owners,
payments and tasks. The [tools reference](../tools.md) lists every tool and what it needs.

Some tools change data in Guesty, for example sending a guest message or updating a calendar. They are
marked "Changes data" in the tools reference, and they work only after an administrator allows changes on
the connection (see [Allow changes](../user-guide.md#allow-changes)). While you are testing, leave changes
off unless you are testing those tools on purpose.

## Day-to-day commands

Run these from the same folder.

| What | Command |
|---|---|
| See the state of the services | `docker compose -f deploy/compose.yaml ps` |
| Read the application's log | `docker compose -f deploy/compose.yaml logs app` |
| Read Caddy's log (certificates) | `docker compose -f deploy/compose.yaml logs caddy` |
| List the people who have accounts | `docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users list` |
| See every admin command | `docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin --help` |

After you change `deploy/.env`, apply the change with `docker compose -f deploy/compose.yaml up -d`.

## If something does not work

- The command stops with a message about `DOMAIN` or `ENCRYPTION_KEYS`: fix `deploy/.env` as the message
  says and run the start command again. The application checks its settings when it starts, and
  `docker compose -f deploy/compose.yaml logs app` names the setting that is wrong.
- The address does not open, or the browser warns about the certificate: check the DNS record, the open
  ports and `ACME_EMAIL`. [TLS and domains](../tls-and-domains.md#if-the-certificate-does-not-appear) lists
  what to check, in order.
- Docker reports `Pool overlaps with other one on this address space`: your server's own network clashes
  with pmsbridge's private network. [TLS and domains](../tls-and-domains.md#when-the-default-address-range-clashes)
  shows the two settings to change.

## Stop it, or remove it

Stop the services and keep everything (the data and the certificates stay):

```
docker compose -f deploy/compose.yaml stop
```

Start them again with `docker compose -f deploy/compose.yaml up -d`.

To remove the services but keep the data:

```
docker compose -f deploy/compose.yaml down
```

To remove everything, including the database and the certificates, add `--volumes`. This cannot be undone:

```
docker compose -f deploy/compose.yaml down --volumes
```

You can then delete the `pmsbridge` folder. The images that were built stay on the server until you remove
them with `docker image rm pmsbridge:local pmsbridge-caddy:local`. If you used the Guesty key only for this
test, delete the key in Guesty too.

## Where your data lives

Everything pmsbridge stores is in three Docker volumes, named like this:

| Volume | What it holds |
|---|---|
| `pmsbridge_app_data` | The database: accounts, connections (the Guesty credentials, encrypted), approvals and the record of what assistants did. |
| `pmsbridge_caddy_data` | Your certificates and the certificate account. Keep it, so restarts do not ask for new certificates. |
| `pmsbridge_caddy_config` | Caddy's own saved settings. |

The settings file `deploy/.env`, with your encryption key, is not in a volume.

**There are no automatic backups yet.** The only copy the application makes by itself is a database copy
taken just before it upgrades its own database, kept in the `app_data` volume. Backup and restore commands
arrive with the installer. Until then, treat this installation as a test: keep your own copy of the
`ENCRYPTION_KEYS` line, and do not rely on the data being there tomorrow.
