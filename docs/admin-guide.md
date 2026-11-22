# Administrator guide

This guide is for the people who run a pmsbridge installation. It starts with signing in and out, because
everything else in the dashboard sits behind it.

## Signing in and out

Open your installation's address (for example `https://mcp.example.com`) in a web browser. If you are not
signed in, you are sent to the sign-in page. Enter your email address and password and select **Sign in**.

- Email addresses are not case sensitive, and spaces around them are ignored. Passwords are case sensitive.
- To sign out, select **Sign out** at the top right of any page. Signing out ends the session on the
  server straight away, so copying the cookie out of the browser afterwards does not help anyone.
- After you sign in, you land on the page you were trying to reach, if it is part of the dashboard.

Passwords must be 12 to 256 characters long. There are no rules about capital letters, digits or symbols: a
long sentence is a good password. Passwords are stored only as argon2id hashes, which cannot be turned back
into the password, and the hashing settings are upgraded automatically the next time you sign in.

### How long you stay signed in

A session ends in whichever of these happens first:

| Rule | Default | Setting |
|---|---|---|
| No activity in the dashboard | 12 hours | `SESSION_IDLE_HOURS` |
| Time since you signed in, even if you stay active | 7 days | `SESSION_MAX_DAYS` |

Closing the browser usually ends the session too, because the cookie is not given an expiry date. (A browser set to "continue where you left off" may keep it.) A session will also end when
an administrator disables your account (disabling accounts arrives with user management). Every sign-in starts a new session: a session from an earlier sign-in
is never reused. See [Configuration](configuration.md) to change the two settings.

### "Email or password is incorrect"

This is the only message you get when a sign-in fails. It deliberately does not say which of the two was
wrong, so nobody can use the sign-in page to find out which email addresses have accounts. It appears when:

- the password is wrong, or the email address has a typo;
- there is no account for that email address;
- the account has been disabled by an administrator (disabling accounts arrives with user management).

If you are sure you typed both correctly, ask an administrator to check that your account exists and is
active.

### "Too many failed sign-in attempts"

Failed sign-ins are counted in two ways, and either one can pause signing in for 5 minutes:

- **Per email address:** 5 failures within 15 minutes for the same email address, no matter where the
  attempts came from. This stops someone from guessing one person's password. A successful sign-in resets
  this count. The side effect is that someone who keeps typing your email address wrongly can keep you
  locked out; they cannot get in, and the pause ends by itself.
- **Per network address:** 20 failures within 15 minutes from the same address, no matter which email
  addresses were tried.

An attempt is counted before its password is checked, so many guesses sent at the same moment cannot slip
past a limit. While signing in is paused, even a correct password is refused. Wait about 5 minutes and try
again. The counts are kept in memory, so restarting the application clears them.

#### Visitors who share an address

Everyone behind the same office router shares one network address. On a standard installation, **every
visitor who reaches the server over IPv6 also shows up as the same address** (the private address of
Docker's gateway), because the internal network is IPv4 only. Private, loopback and link-local addresses
(for example `10.x.x.x`, `172.16.x.x` to `172.31.x.x`, `192.168.x.x`, `100.64.x.x` to `100.127.x.x`,
`127.x.x.x`, `fc00::/7` and `fe80::/10`) therefore get a much higher limit of 200 failures per 15 minutes,
so a few mistyped passwords from one IPv6 visitor cannot lock out all the others. The limit per email
address stays at 5 failures for everyone, and it is what protects each account.

If the internal network later becomes dual-stack, IPv6 visitors are told apart by their network (the first
64 bits of the address) and the normal limit of 20 applies to them.

### If sign-in keeps failing

| What you see | What to do |
|---|---|
| "Email or password is incorrect." | Check the email address and password. See above for what this message covers. |
| "The request came from an unexpected address." | Open the site at exactly the address in `PUBLIC_BASE_URL` (for example `https://mcp.example.com`), not by IP address and not through another name for the same server. The browser must be on the same address you configured. |
| "The form has expired. Reload the page and try again." | Reload the sign-in page and try again. Cookies must be allowed for the site: the form is tied to a cookie that the browser sets when the page loads. A page that has been open for more than an hour also expires. |
| "Too many failed sign-in attempts." | Wait about 5 minutes. |
| You are sent back to the sign-in page after a correct password | The browser did not keep the sign-in cookie. Make sure you use the HTTPS address and that cookies are allowed. |

`PUBLIC_BASE_URL` must be exactly the HTTPS address you type in the browser, including the port when it is
not 443 (for local testing, for example `https://localhost:8443`). See [TLS and domains](tls-and-domains.md).

## Trying it on your own computer

The sign-in cookie is marked `Secure`, so browsers only keep it over HTTPS. This is on purpose, and there is
no setting to turn it off. To try the dashboard on your own computer, run it with `ENVIRONMENT=development`
and the internal certificate mode described in [TLS and domains](tls-and-domains.md), and open the HTTPS
address. Some browsers accept `Secure` cookies for `http://localhost`, but others do not, so do not rely on it:
if you keep landing back on the sign-in page after entering the right password over `http://`, switch to the
HTTPS address.

## Creating accounts

Creating the first administrator, inviting people, roles and two-step verification are not available yet and
will be described here when they are.
