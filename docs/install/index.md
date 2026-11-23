# Install pmsbridge

One command installs pmsbridge on a fresh Linux server. It checks the server, installs Docker if it is
missing, opens the web ports in the server's firewall, downloads pmsbridge, asks for two things (your domain
name and your email address), checks your DNS record, starts everything, and prints a one-time link for
creating the first administrator.

You do not edit any file.

## What you need

- **A server** running Ubuntu 22.04 or newer, or Debian 12 or newer (Intel/AMD or ARM). At least 2 GB of
  memory and 10 GB of free disk space are comfortable. You need root access (the installer is started with
  `sudo`).
- **A domain you already own**, and access to its DNS settings. AI assistants only connect to an address with
  a real certificate, so a real domain name is required. [TLS and domains](../tls-and-domains.md) explains why.
- **Ports 80 and 443 open to the internet**, in the server's firewall and in any firewall at your hosting
  provider (often called a "security group"). Port 80 is needed to get the certificate.
- **An email address.** The certificate authority uses it to contact you about your certificate, and the
  first administrator account is created for it.
- Later, a Guesty API key (a client ID and a client secret) created just for pmsbridge. The
  [user guide](../user-guide.md#connect-guesty-step-by-step) explains how.

## Step 1: point a name at the server

pmsbridge needs its own web address, such as `mcp.yourcompany.com`. You make that address lead to your
server by creating one record in your domain's DNS settings. The company that manages your DNS is often the
company where you bought the domain.

**Use a subdomain of a domain you already own**, for example `mcp.yourcompany.com`, rather than the main
domain. Your main website keeps working untouched.

### A worked example

Suppose you own `acme-stays.com`, your website lives at `acme-stays.com`, and your new server's public
address is `203.0.113.10`. You choose `mcp.acme-stays.com` for pmsbridge. In your DNS settings you add one
record:

| Type | Name (host) | Value (points to) | TTL |
|---|---|---|---|
| A | mcp | 203.0.113.10 | Auto (or 300) |

Nothing else changes: `acme-stays.com` and `www.acme-stays.com` still go where they went before.

Things to know:

- **"Name" is only the subdomain part.** Some DNS providers want just `mcp`, others want the full
  `mcp.acme-stays.com`. If your provider shows your domain next to the Name box, type only `mcp`. If you are
  not sure, use the one the provider's own examples use.
- **Use an A record.** A CNAME record is only for the case where your server already has a hostname from
  your hosting provider (for example `server123.provider.net`). Then you create Type CNAME, Name `mcp`,
  Value `server123.provider.net` instead. **Never use a CNAME on your bare main domain** (`acme-stays.com`
  itself): DNS does not allow it next to your other records, and it can break your email.
- **Add an AAAA record only if your server has a public IPv6 address.** It has the same Name and the server's
  IPv6 address as its Value. If your name already has an AAAA record that points somewhere else, remove it or
  point it at this server, otherwise the certificate may fail.
- **Your server's public address** is shown in your hosting provider's control panel. On some cloud servers the
  server itself only knows a private address (it starts with `10.`, `172.16.` to `172.31.` or `192.168.`);
  the public address is still the one in the control panel. The installer shows your address when it can
  work it out from the server's own network settings, and a placeholder when it cannot. It never asks a web
  service for it.
- **DNS changes usually take a few minutes, sometimes up to an hour.** You can create the record before or
  while you run the installer: it checks the record and waits for it.

To check from any computer: `nslookup mcp.acme-stays.com` should answer with your server's address.

## Step 2: run the installer

Connect to the server (for example with `ssh`) and run:

```
curl -fsSL https://raw.githubusercontent.com/faridmostafaei259/pmsbridge/main/install.sh | sudo bash
```

You can also download the project and run `sudo ./install.sh` from inside it. Either way the installer asks
you questions on the keyboard, so it works when started this way.

### What it asks

Only two things:

1. **The domain name**, for example `mcp.acme-stays.com`. Enter just the name, with no `https://`, no port and
   no path. Before it asks, the installer prints the DNS explanation and record from step 1, filled in with
   your server's address.
2. **Your email address.**

It then shows what it will use and asks you to confirm.

To skip the questions (for example in a script), pass the answers as options:

```
curl -fsSL https://raw.githubusercontent.com/faridmostafaei259/pmsbridge/main/install.sh \
  | sudo bash -s -- --domain mcp.acme-stays.com --email you@acme-stays.com --yes
```

### What it does

The installer prints "Step N of 10" for each stage. If a stage fails it says which one, why, and what to do.

1. **Checks the server.** It looks at the system and processor, and at memory and disk space.
2. **Installs Docker** from Docker's official package repository if Docker or its Compose plugin is missing,
   and turns it on at boot.
3. **Opens the firewall.** If `ufw` or `firewalld` is active, it allows ports 80 and 443. It cannot see your
   hosting provider's firewall, so it reminds you to check that one.
4. **Gets the code.** It downloads pmsbridge into `/opt/pmsbridge`.
5. **Asks for your domain and email** and confirms them.
6. **Checks your DNS record.** If the name does not point at this server yet, it shows the exact record to
   create and checks again every 30 seconds for up to 30 minutes.
7. **Writes the settings** to `/opt/pmsbridge/deploy/.env`, readable by root only, including a new
   encryption key. It then shows a one-time reminder to back that key up.
8. **Starts pmsbridge** (building it the first time, which takes a few minutes) and waits until
   `https://your-domain/healthz` answers. The certificate is obtained automatically at this point.
9. **Creates the first administrator** for your email address.
10. **Prints the dashboard address, the MCP address and a one-time setup link.**

### Options

| Option | What it does |
|---|---|
| `--domain NAME` | The domain name, so the installer does not ask. |
| `--email ADDRESS` | Your email address, so the installer does not ask. |
| `--admin-email ADDRESS` | A different email address for the first administrator (default: `--email`). |
| `--dir PATH` | Where to install (default `/opt/pmsbridge`). |
| `--ref NAME` | The branch or version to install (default `main`). |
| `--tls auto` or `--tls internal` | `auto` (default) gets a real certificate. `internal` is for testing on your own machine: assistants and browsers do not trust it, and no DNS record is needed. |
| `--no-install-docker` | Do not install Docker. The installer stops if it is missing. |
| `--no-firewall` | Do not change the server's firewall. |
| `--no-wait-dns` | Do not wait for the DNS record. The certificate cannot be issued until it is correct. |
| `--yes` | Accept the defaults and do not ask to confirm. Missing values are still asked for. |
| `--help` | Show the options. |

If there is no keyboard (for example a script that runs the installer without a terminal) and the domain or
email was not given, the installer stops before changing anything and prints the option to add.

## After the installer finishes

1. **Open the setup link** in your browser within 24 hours and choose a password. The link is printed only on
   the screen. It is not saved in the log file, so copy it before you close the window. If you lose it, see
   [the administrator guide](../admin-guide.md#first-sign-in-the-setup-link).
2. **Set up two-factor sign-in** with an authenticator app and save the recovery codes. Administrators must
   use two-factor sign-in.
3. **Back up your encryption key.** It protects the Guesty credentials pmsbridge stores. To show it, run
   `sudo grep ENCRYPTION_KEYS /opt/pmsbridge/deploy/.env` and save the line in a password manager. If you lose
   it, stored credentials cannot be recovered and you would enter them again.
4. **Add your Guesty connection.** See the [user guide](../user-guide.md#connect-guesty-step-by-step).
5. **Connect an AI assistant** to `https://your-domain/mcp`. See [Connect an AI assistant](../connect-ai-clients.md).

## Running the installer again

It is safe to run again, for example after fixing a DNS record or a closed port:

- The encryption key and your data are never replaced. The installer keeps every setting in `deploy/.env`,
  and changes only the domain and email when you give new ones.
- If pmsbridge data from an earlier installation exists but the key is missing, the installer stops instead
  of creating a new key that could not read that data.
- If the first administrator already exists, no second one is created and the installer shows how to get a
  new password link.
- When run from the web, it also updates the downloaded code in `/opt/pmsbridge` to the latest version of
  the branch (or to `--ref`) and rebuilds.

## Where things live

| What | Where |
|---|---|
| Code and settings | `/opt/pmsbridge` (settings and the encryption key: `/opt/pmsbridge/deploy/.env`) |
| The installer's log | `/var/log/pmsbridge-install.log` (root only; never holds the key or the setup link) |
| The database, certificates | Docker volumes `pmsbridge_app_data`, `pmsbridge_caddy_data`, `pmsbridge_caddy_config` |

## Day-to-day commands

Run these in `/opt/pmsbridge`.

| What | Command |
|---|---|
| See the services | `sudo docker compose -f deploy/compose.yaml ps` |
| Read the application's log | `sudo docker compose -f deploy/compose.yaml logs app` |
| Read Caddy's log (certificates) | `sudo docker compose -f deploy/compose.yaml logs caddy` |
| Stop (keeps everything) | `sudo docker compose -f deploy/compose.yaml stop` |
| Start again | `sudo docker compose -f deploy/compose.yaml up -d` |

pmsbridge starts again by itself after a reboot.

## If something fails

- **"is not supported"**: use Ubuntu 22.04 or newer, or Debian 12 or newer. On another system, install Docker
  Engine and the Compose plugin yourself first; the installer then continues.
- **It waits for DNS and gives up**: create the record shown on the screen, wait a few minutes, and run the
  installer again.
- **The address does not answer or the certificate is missing**: check the DNS record, that ports 80 and 443
  are open (also at your hosting provider), and your email address. [TLS and domains](../tls-and-domains.md#if-the-certificate-does-not-appear)
  lists what to check, in order.
- **A port is already in use**: another program, often a web server such as Apache or nginx, uses port 80 or
  443. Stop it, then run the installer again.
- **"Pool overlaps with other one on this address space"**: see
  [TLS and domains](../tls-and-domains.md#when-the-default-address-range-clashes).
- Anything else: the full output of every step is in `/var/log/pmsbridge-install.log`.

## Remove pmsbridge

There is no uninstall command yet. To remove everything, including the database and certificates (this cannot
be undone, so back up first):

```
cd /opt/pmsbridge
sudo docker compose -f deploy/compose.yaml down --volumes
sudo docker image rm pmsbridge:local pmsbridge-caddy:local
cd / && sudo rm -rf /opt/pmsbridge /var/log/pmsbridge-install.log
```

To remove only the services and keep the data, leave out `--volumes` and do not delete the folder.
Docker itself stays installed, and any firewall rules the installer added stay open. If you used the Guesty key
only for pmsbridge, delete the key in Guesty too.

## Installing by hand

If you cannot use the installer, [Try pmsbridge on a test server](test-server.md) shows the same steps one by
one.
