# TLS and domains

pmsbridge is reached over HTTPS. A small web server called Caddy sits in front of the application, handles
HTTPS and certificates for you, and passes requests on to the application. You never create, install or
renew a certificate by hand.

## Why a trusted certificate matters

AI assistants connect to your pmsbridge from the internet. They only accept an address that:

- is reachable from the public internet over HTTPS, and
- uses a certificate that a public certificate authority has signed (a "publicly trusted" certificate).

A self-signed certificate, or one from a private authority, is rejected by both. That is why the default
mode below obtains a real certificate for your domain, and why the `internal` mode is only for testing on
your own machine.

## Settings

The stack reads its settings from a file named `.env` next to `deploy/compose.yaml`. The installer will
create this file for you. Until then, create it by hand. Never commit it or share it.

| Setting | Required | What it does |
|---|---|---|
| `DOMAIN` | yes | The host name people use, for example `mcp.example.com`. No `https://`, no path. |
| `PUBLIC_BASE_URL` | yes | The full address, for example `https://mcp.example.com`. It must match `DOMAIN`. |
| `ENCRYPTION_KEYS` | yes | The key that encrypts stored PMS credentials. Create it once with the command in the [configuration reference](configuration.md) and keep a copy in a password manager: without it, stored credentials can't be recovered. |
| `TLS_MODE` | no | `auto` (the default) or `internal`. See below. |
| `ACME_EMAIL` | yes for `auto` | An address the certificate authority can contact about your certificate. |
| `ACME_CA` | no | `production` (the default) or `staging`. See "Testing without limits". |
| `HTTP_PORT`, `HTTPS_PORT` | no | The ports opened on the server. Default 80 and 443. Change them only for testing. |
| `BACKEND_SUBNET`, `CADDY_BACKEND_IP` | no | The private network between Caddy and the application. Change only if the default clashes with a network you use. See "How requests reach the application". |

## Starting and stopping the stack

Run these commands in the install directory, the folder that contains `deploy/` (the installer will place
it in `/opt/pmsbridge`). The `.env` file is read from `deploy/`.

Start (the first start also builds the images, which takes a minute or two):

```
docker compose -f deploy/compose.yaml up -d --wait
```

The command returns when both services report healthy. The last lines look like this:

```
 Container pmsbridge-app-1 Healthy
 Container pmsbridge-caddy-1 Healthy
```

Check the state at any time:

```
docker compose -f deploy/compose.yaml ps
```

Both services should show `healthy`:

```
NAME                SERVICE   STATUS
pmsbridge-app-1     app       Up 11 seconds (healthy)
pmsbridge-caddy-1   caddy     Up 5 seconds (healthy)
```

Stop (keeps all data and certificates):

```
docker compose -f deploy/compose.yaml stop
```

```
 Container pmsbridge-caddy-1 Stopped
 Container pmsbridge-app-1 Stopped
```

Do not add `--volumes` to a `down` command. That deletes the data and the certificates.

## Mode `auto` (the default)

Use this on a server with a public IP address where ports 80 and 443 are open to the internet.

What you do, once:

1. Create a DNS record at your domain provider that points your domain at the server:

    | Type | Name | Value |
    |---|---|---|
    | `A` | `mcp` (for `mcp.example.com`) | the server's public IPv4 address |
    | `AAAA` | `mcp` | the server's public IPv6 address (only if the server has one) |

2. Make sure the server accepts traffic on ports 80 and 443. If the server sits behind a firewall or a
   cloud security group, allow both ports. Port 80 is needed for the certificate authority to confirm that
   you control the domain, and to redirect plain `http://` visits to `https://`.
3. Set `TLS_MODE=auto`, `DOMAIN`, `PUBLIC_BASE_URL` and `ACME_EMAIL` in `.env`, then start the stack.

What happens next, without you:

- On first start Caddy asks Let's Encrypt for a certificate. If DNS has not reached the server yet, Caddy
  keeps retrying until it has. If Let's Encrypt cannot be used, Caddy falls back to ZeroSSL.
- Certificates are renewed automatically, about 30 days before they expire. Nobody has to remember.
- Certificates and the certificate account are kept in a Docker volume (`pmsbridge_caddy_data`). Restarts,
  reboots and updates reuse them, so you do not hit certificate-authority rate limits. Do not delete that
  volume.
- Visits to `http://` are redirected to `https://`, and browsers are told to insist on HTTPS
  (the `Strict-Transport-Security` header) because a publicly trusted certificate is in use.

Moving the instance to a new server is the only time the DNS record changes: restore a backup on the new
server, point the record at it, and the certificate is issued again by itself.

## Mode `internal` (testing only)

Caddy signs the certificate itself with a private authority. Browsers and AI assistants do not trust it.
Use it to try pmsbridge on a laptop or in automated tests.

```
DOMAIN=localhost
PUBLIC_BASE_URL=https://localhost:8443
TLS_MODE=internal
HTTP_PORT=8080
HTTPS_PORT=8443
```

After starting the stack, check it with `curl -k https://localhost:8443/healthz`. The `-k` flag tells curl
to accept the private certificate. You should see `{"status":"ok"}`.

When you use ports other than 80 and 443, the automatic redirect from `http://` sends visitors to the
standard HTTPS port, so open the `https://` address with the port directly. This only affects testing.

The `internal` mode never sends the HSTS header, because the certificate is not publicly trusted.

## Testing without limits

Certificate authorities limit how many real certificates they issue for one domain. If you are
experimenting with a domain (repeated installs, firewall changes), set `ACME_CA=staging`. The staging
authority has much higher limits, but its certificates are not trusted by browsers or AI apps. Switch back to
`production` (or remove the line) for real use. Any other value stops Caddy with an error that names the
unknown value.

## If the certificate does not appear

Check these in order:

1. The DNS record exists and points at this server. From any computer: `nslookup mcp.example.com`.
2. Ports 80 and 443 are open from the internet (server firewall and any cloud firewall).
3. `ACME_EMAIL` is set. Without it Caddy refuses to start in `auto` mode.
4. Look at what Caddy says: `docker compose -f deploy/compose.yaml logs caddy`. Look for lines mentioning
   `obtain`, `challenge` or `error`.

Caddy keeps retrying with growing pauses, so a certificate normally arrives soon after the cause is fixed.

## How requests reach the application

Only Caddy listens on the server's ports. The application runs on a private network shared with Caddy and
publishes no port of its own, so nothing reaches it except through Caddy. Caddy runs as an unprivileged
user with all extra permissions removed.

The application learns the visitor's address from headers that Caddy adds, so it must only believe those
headers when they come from Caddy. The private network therefore has a fixed address range
(`172.31.250.0/24`) and Caddy always has the same address on it (`172.31.250.10`). The application trusts
that one address and no other, not even the rest of the range. This is set up for you.

If you change `TRUSTED_PROXIES` in `.env`, list only the addresses of your own reverse proxy. A value that
trusts every address (`*`, `0.0.0.0/0` or `::/0`), or a very large network, would let any visitor pretend to
be someone else, so the application refuses to start with it in production. It prints one line naming
`TRUSTED_PROXIES` and stops.

With Docker Compose, this looks like a site that is down: the application keeps restarting and Caddy has
nothing to forward to. To see the line, run `docker compose -f deploy/compose.yaml logs app`. To fix it,
remove the `TRUSTED_PROXIES` line from `.env` (the default is Caddy's own address), or set it to Caddy's
address (`172.31.250.10` unless you changed `CADDY_BACKEND_IP`), then start the stack again with
`docker compose -f deploy/compose.yaml up -d`.

### When the default address range clashes

The default range, `172.31.250.0/24`, sits inside `172.31.0.0/16`, which is the range of the default
network (VPC) on AWS. It can also overlap other private networks, such as a company VPN. A clash shows up
in one of two ways:

- Starting the stack fails with an error such as
  `Pool overlaps with other one on this address space`. This happens, for example, on an AWS server whose
  own subnet is `172.31.240.0/20`, because that subnet contains `172.31.250.0/24`.
- The stack starts, but the application cannot reach other machines in the clashing range.

In both cases, choose a free private range and set both values in `.env` before the first start. The
address must lie inside the range. For example:

```
BACKEND_SUBNET=10.99.0.0/24
CADDY_BACKEND_IP=10.99.0.10
```

Then start the stack again. If you already started it once with the old range, run
`docker compose -f deploy/compose.yaml down` first (without `--volumes`) so the network is created again
with the new range.

### Visitors using IPv6

The private network is IPv4 only. Visitors can still reach your server over IPv6, because Docker passes
that traffic to Caddy through its built-in proxy (docker-proxy). The side effect is that Docker rewrites
the source address of IPv6 visitors: the application sees all of them as coming from the network's
gateway address (`172.31.250.1` by default) instead of their own. Anything that depends on the visitor's
address, such as log entries or per-address limits, therefore treats all IPv6 visitors as one visitor.
IPv4 visitors are not affected. Sign-in and AI access work the same over both. A dual-stack option that
keeps real IPv6 addresses is planned.

Caddy also limits request bodies to 2 MB and does not expose the internal readiness endpoint
(`/readyz`) to the internet. The application has its own limit of 2 MiB per request body (a larger one gets
the answer `413`), so the limit also holds for traffic that reaches the application without Caddy. The
`/healthz` endpoint answers `{"status":"ok"}` and is safe to use for uptime monitoring.

### What Caddy writes to its log

Caddy logs its own messages, such as an error when the application is not reachable, to the container's
output (`docker compose -f deploy/compose.yaml logs caddy`). Web addresses can carry one-time values, so
these log lines keep the path of a request but leave out everything after the `?` (the query string). They
also leave out all request headers, so tokens, session values and similar headers are never written. The
log does not record every request, only problems.

## Other modes (coming)

Three more modes are planned. They are not available yet.

- `dns`: for servers behind a firewall that cannot accept port 80. The certificate is confirmed through a
  DNS record at your DNS provider instead.
- `custom`: use a certificate and key issued by your own company.
- `proxy`: run behind a reverse proxy, load balancer or tunnel that you already have.
