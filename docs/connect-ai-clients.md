# Connect an AI assistant

This page shows how to let an AI assistant use pmsbridge. You do it once per assistant, and each person
signs in with their own pmsbridge account, so the assistant only ever acts for the person who approved it.

You need:

- A running pmsbridge with at least one PMS connection (see the [user guide](user-guide.md)).
- A pmsbridge account (your administrator invites you).
- An AI assistant that can use remote MCP servers ("connectors" or "custom connectors" in most assistants).

## The address to use

The address of your pmsbridge is your domain followed by `/mcp`:

```
https://<your-domain>/mcp
```

For example, if you open pmsbridge at `https://pms.example.com`, the address is
`https://pms.example.com/mcp`. Type it exactly like this, with `https://` and without a slash at the end.

Most AI assistants call your server from the internet, not from your own computer. That means:

- pmsbridge must be reachable from the internet at your domain.
- It must have a real certificate (`TLS_MODE=auto`, the default). The `internal` test certificate is not
  trusted by assistants. See [TLS and domains](tls-and-domains.md).

## Add it to your assistant

The exact menu names differ between assistants. In general:

1. Open your assistant's settings and find where you add a **connector**, **custom connector** or
   **remote MCP server**.
2. Choose to add a new one. Give it a name you will recognize, such as "pmsbridge".
3. Paste the address (`https://<your-domain>/mcp`).
4. You do not need to enter a client ID or a secret. Leave those fields empty if the assistant shows them.
5. Select **Add**, then **Connect** (some assistants call it **Authorize** or **Sign in**).

## Sign in and approve

Your assistant opens a page from your pmsbridge in your browser.

1. **Sign in** with your pmsbridge e-mail and password. Administrators also type their two-factor code.
   If you are already signed in to pmsbridge in that browser, this step is skipped.
2. **Check the approval page.** It names the assistant and the website it will return you to. If either
   looks wrong, choose **Cancel**.
3. **Choose the connections** the assistant may use: all of them (including ones added later), or only the
   ones you tick. An assistant can never use a connection you did not choose.
4. Choose **Allow access**. Your browser goes back to the assistant, and the connection is ready.

You can then ask the assistant things like "which properties do we have?" or "who is checking in tomorrow?".
The assistant's tools are listed in the [tools reference](tools.md).

## Change or end an approval

Open **Connected apps** in pmsbridge to see every assistant you approved. There you can change which
connections each one may use, or remove it. Removing it takes effect at once: the assistant is signed out
and its next request is refused. Your administrator can also end AI access for a person or an assistant
(see [`oauth revoke-ai-access`](admin-guide.md#the-command-oauth-revoke-ai-access)).

Access also ends at once if your pmsbridge account is turned off, your password or two-factor sign-in is
reset, or your role changes.

Assistants stay signed in without asking you again, up to 30 days after they were last used (and no more
than 90 days after you approved them). After that you approve again.

## If something does not work

| What you see | What it means |
|---|---|
| The assistant says it cannot connect, or the address is rejected | Check the address ends in `/mcp`, starts with `https://`, and that you can open `https://<your-domain>/healthz` in a browser from outside your network. |
| The assistant keeps asking you to sign in | Your approval ended (removed, turned off, or expired). Connect it again. |
| "No connections are available" | You approved it without choosing a connection, or none exist yet. Open **Connected apps** and choose connections, or ask an administrator to add one. |
| "There is no connection with that name or id" | The assistant asked for a connection it is not allowed to use, or one that does not exist. Ask it to run `list_connections` and use a name from that list. |
| "Changes are not allowed" | The tool changes data and an administrator has not allowed changes on that connection. See [Allow changes](user-guide.md#allow-changes). |
| "The connection ... is not available right now (it is turned off)" | An administrator turned the connection off on the **Connections** page. Ask them to turn it on again. |
| "Several connections are available" | You approved more than one connection, so the assistant must say which one. Tell it the connection name, or ask it to run `list_connections` first. |
| "The connection ... needs attention" | The PMS rejected pmsbridge's credentials. An administrator must fix it on the **Connections** page. |
| "The outcome is unknown" after a change | The request took too long. Check the property management system before asking again; pmsbridge never repeats a change by itself. |

Every tool call is recorded in a tool-call record in the pmsbridge database on your server: who, which
assistant, which connection, which tool, whether it worked and how long it took. The record never holds the
details of a request, guest names or any tokens, and entries are deleted after `AUDIT_RETENTION_DAYS` days
(90 by default). A page and a command for viewing it come in a later release; until then it is kept for
investigations and is included in backups. Administrator actions (adding connections, revoking access) are
written to the application log separately.
