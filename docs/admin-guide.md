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

## First sign-in: the setup link

A new installation has no accounts. The first administrator is created from inside the running container,
and the command prints a one-time **setup link**:

```bash
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users create-admin --email you@example.com
```

The only thing the command prints is the link, which looks like `https://mcp.example.com/setup?token=...`.
Open it in your browser within 24 hours.

**What exists today.** The simpler host command `pmsbridge create-admin` arrives with the installer and
command-line tool and is not available yet. The in-container entry point used above,
`python -m pmsbridge.admin`, also arrives with that packaging work. Until it does, the command above does not
run on a fresh image, even though the `users create-admin` action itself is already built and tested.

What happens next:

1. **Choose a password** (12 to 256 characters, typed twice). Opening the link does not use it up, and the
   address you are sent on to has no secret in it, so it does not stay in your browser history.
2. **Turn on two-factor sign-in.** Administrators must have it. The page shows a picture (a QR code) and a text
   key. Scan the picture with an authenticator app, or type the key into the app by hand, then enter the
   6-digit code the app shows. The account only becomes active when the code is right.
3. **Save your recovery codes.** The page shows 10 codes, once. See below.
4. Sign in with your email address, your password and a code from the app.

The link works once. It also stops working after 24 hours (it must still be valid when you finish, not only
when you open it), and when you ask for another link for the same
address (running `create-admin` again for someone who has not finished setup prints a new link and cancels the
old one). The command refuses an email address that already has an active account.
Anyone who sees the link can set up the account, so send it only to the person who needs it, and run the
command again if you are unsure who has seen it.

### Forgotten administrator password

```bash
docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users reset-admin-password --email you@example.com
```

This prints a one-time link to `/reset`. The person opens it and chooses a new password. Their two-factor
sign-in stays on, and every session they had ends. The link has the same rules as the setup link.

## Two-factor sign-in

Once two-factor sign-in is on, signing in has two steps: your email and password, then a 6-digit code from your
authenticator app (or a recovery code). The code changes every 30 seconds. A code works once, so waiting for the
next one is normal if you sign in twice in a row. The first step does not give access on its own: nothing in the
dashboard opens until the second step is done, and the second step must be finished within 5 minutes.

- **Administrators** must use it. An administrator whose two-factor sign-in is missing is sent to the setup page
  right after the password step, and gets no session until it is done.
- **Other people** can turn it on or off for themselves on the **Account security** page (the link is on the
  home page). Turning it off needs your password and a current code.
- If the code is wrong five times in 15 minutes, further codes are refused for that person for 15 minutes,
  even correct ones. Starting the sign-in again does not reset this. There is also a daily cap: 20 wrong
  codes in 24 hours lock the person out of the code step for 24 hours, and a correct code does not reset that
  count. Both limits are kept in memory, so restarting the application clears them.
- The code secret is stored encrypted with your `ENCRYPTION_KEYS`. If that setting is missing, the setup page
  says two-factor sign-in cannot be set up. Fix the setting first.

Use any authenticator app that supports one-time codes (time based, 6 digits, 30 seconds). Phone built-in
password managers and most standalone apps do.

### If the code step fails

| What you see | What to do |
|---|---|
| "That code is not correct." although you typed what the app shows | The clock of your phone or of the server is off by more than about 30 seconds. Set the phone to automatic date and time. On the server, keep time synchronisation on (for example `timedatectl` should report the clock as synchronised). A code from the previous or next 30-second step is also accepted, but no more. |
| The same code is refused a second time | Each code works once. Wait for the app to show the next one. |
| "Too many wrong codes. Wait 15 minutes and try again." | See the limits above. A correct code does not help while the pause lasts. |
| A server error (or "no usable encryption key") at the code step or when setting up | `ENCRYPTION_KEYS` in `.env` is missing, or no longer holds the key that was active when the authenticator was set up. Restore the correct key line and restart. Recovery codes still work in the meantime, because they do not need the key. |
| Wrong password or code on **Account security** | Mistakes there (turning two-factor off, making new recovery codes) count towards the same limits as the code step at sign-in. |

### Recovery codes

Ten codes are shown once when you turn two-factor sign-in on. Each works one time in place of an app code. Keep
them somewhere other than your phone, such as a password manager or a printout in a safe place. The server keeps
only a scrambled form of each code, so nobody can show them to you again.

To get a fresh set, open **Account security**, enter your password and a current code, and select **Make new
recovery codes**. The old codes stop working immediately. Typing a code with spaces, dashes or lower case
letters is fine.

### If you lose your phone

- **You have a recovery code:** at the code step, enter a recovery code instead. It works once. Then open
  **Account security** and make new recovery codes. To move to a new phone, turn two-factor sign-in off and
  on again. Staff can do this themselves; administrators cannot turn it off, so they will ask another
  administrator to reset it once user management arrives.
- **You have no recovery code:** another administrator has to reset your two-factor sign-in. That action
  (`users reset-2fa`) arrives with user management in the next release and is not available yet. Until then
  there is no supported way back in, so store your recovery codes safely when you first see them.

## Creating accounts

Inviting people, roles, and disabling accounts are not available yet and will be described here when they are.
