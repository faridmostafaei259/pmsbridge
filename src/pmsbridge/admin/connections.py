"""PMS connections, run in the container as `python -m pmsbridge.admin connections <action>`.

Actions:

- `list` prints every connection: id, name, system, status, whether changes are allowed, tokens
  left today (Guesty) and the last successful call.
- `add --label <name> --client-id <id>` connects a Guesty account. The client secret is read from
  a hidden prompt, or from standard input with `--from-stdin` (the first line). It is never an
  argument. Checking the credentials uses one of the key's 5 daily Guesty tokens; the token is kept.
- `rotate --connection <id or name> --client-id <id>` replaces a connection's credentials the same
  way. The connection keeps its id, so assistant approvals keep working. The old credentials stay
  in use unless the new ones are accepted.
- `test --connection <id or name>` sends one read-only request with the stored token. It never
  asks Guesty for a token.
- `disable` and `enable` turn a connection off and on.
- `allow-changes` and `stop-changes` switch AI assistants' write actions on or off for one
  connection (off by default). Allowing needs `--yes`: the assistant can then change prices,
  availability and send guest messages, on the operator's responsibility.
- `remove --connection <id or name> --yes` deletes the connection, its token and credentials, and
  takes it off every assistant approval.

Secrets are never printed or logged. Each action writes an audit line (the acting user is `cli`)
with the connection id only.

Exit codes: 0 done, 1 refused or failed (for example the key is already connected, Guesty did not
accept the credentials, the test failed), 2 bad input, configuration or a database that cannot be
used.
"""

import argparse
import getpass
import sys
from collections.abc import Callable, Coroutine
from datetime import datetime

from pmsbridge.accounts.management import ActionRefusedError
from pmsbridge.admin.users import run_action
from pmsbridge.core import storage
from pmsbridge.pms import connections
from pmsbridge.pms.http import close_http_clients

ACTING = "cli"
STATUS_TEXT = connections.STATUS_TEXT


class UsageError(Exception):
    """Bad command input; the message is shown and the exit code is 2."""


def _prepare() -> None:
    storage.import_model_modules(storage.MODEL_MODULES)


async def list_text() -> str:
    _prepare()
    rows = await connections.list_connections()
    if not rows:
        return "There are no connections yet."

    def when(value: datetime | None) -> str:
        return value.strftime("%Y-%m-%d %H:%M UTC") if value else "never"

    table = [("ID", "NAME", "SYSTEM", "STATUS", "CHANGES", "TOKENS LEFT", "LAST SUCCESS")] + [
        (
            row.id,
            row.label,
            row.provider,
            STATUS_TEXT[row.status],
            "allowed" if row.writes_enabled else "off",
            "-" if row.budget_remaining is None else str(row.budget_remaining),
            when(row.last_success_at),
        )
        for row in rows
    ]
    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in table
    )


async def add(label: str, client_id: str, secret: str) -> str:
    _prepare()
    try:
        connection_id = await connections.add_guesty(label, client_id, secret, ACTING)
    finally:
        await close_http_clients()
    return f"The connection was added. Connection id: {connection_id}"


async def rotate(reference: str, client_id: str, secret: str) -> str:
    _prepare()
    try:
        connection_id = await connections.replace_guesty_credentials(
            reference, client_id, secret, ACTING
        )
    finally:
        await close_http_clients()
    return f"The credentials were replaced. Connection id: {connection_id} (unchanged)"


async def test(reference: str) -> str:
    _prepare()
    try:
        ok, message = await connections.test_connection(reference, ACTING)
    finally:
        await close_http_clients()
    if not ok:
        raise ActionRefusedError(message)
    return message


async def disable(reference: str) -> str:
    _prepare()
    await connections.set_enabled(reference, False, ACTING)
    return "The connection is turned off."


async def enable(reference: str) -> str:
    _prepare()
    await connections.set_enabled(reference, True, ACTING)
    return "The connection is turned on."


async def allow_changes(reference: str) -> str:
    _prepare()
    await connections.set_writes_enabled(reference, True, ACTING)
    return f"Changes are now allowed on this connection.\n{connections.ALLOW_CHANGES_WARNING}"


async def stop_changes(reference: str) -> str:
    _prepare()
    await connections.set_writes_enabled(reference, False, ACTING)
    return "Changes are no longer allowed on this connection."


async def remove(reference: str) -> str:
    _prepare()
    await connections.remove_connection(reference, ACTING)
    return "The connection was removed."


def read_secret(*, from_stdin: bool) -> str:
    """The client secret: the first line of standard input, or a hidden prompt."""
    if from_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    elif sys.stdin.isatty():
        value = getpass.getpass("Client secret (hidden): ")
    else:
        raise UsageError("There is no terminal to ask for the secret. Use --from-stdin.")
    if not value:
        raise UsageError("The client secret is empty.")
    return value


def _text(args: argparse.Namespace, name: str) -> str:
    return str(getattr(args, name))  # pyright: ignore[reportAny]


def _flag(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name))  # pyright: ignore[reportAny]


def _run(work: Callable[[], Coroutine[object, object, str]]) -> int:
    return run_action(work)


def _need_yes(args: argparse.Namespace, why: str) -> int | None:
    if _flag(args, "yes"):
        return None
    sys.stderr.write(f"Error: {why} Add --yes to confirm.\n")
    return 2


def _with_secret(
    args: argparse.Namespace, work: Callable[[str], Coroutine[object, object, str]]
) -> int:
    try:
        secret = read_secret(from_stdin=_flag(args, "from_stdin"))
    except UsageError as problem:
        sys.stderr.write(f"Error: {problem}\n")
        return 2
    return _run(lambda: work(secret))


def _run_list(_args: argparse.Namespace) -> int:
    return _run(list_text)


def _run_add(args: argparse.Namespace) -> int:
    label, client_id = _text(args, "label"), _text(args, "client_id")
    return _with_secret(args, lambda secret: add(label, client_id, secret))


def _run_rotate(args: argparse.Namespace) -> int:
    reference, client_id = _text(args, "connection"), _text(args, "client_id")
    return _with_secret(args, lambda secret: rotate(reference, client_id, secret))


def _simple(
    work: Callable[[str], Coroutine[object, object, str]],
) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        reference = _text(args, "connection")
        return _run(lambda: work(reference))

    return run


def _run_allow(args: argparse.Namespace) -> int:
    refused = _need_yes(
        args, "Allowing changes lets AI assistants change data in Guesty on your responsibility."
    )
    return refused if refused is not None else _simple(allow_changes)(args)


def _run_remove(args: argparse.Namespace) -> int:
    refused = _need_yes(args, "Removing deletes the connection, its token and its credentials.")
    return refused if refused is not None else _simple(remove)(args)


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
) -> None:
    parser = subparsers.add_parser("connections", help="PMS connections.")
    actions = parser.add_subparsers(dest="action", required=True)

    def add_action(
        name: str,
        handler: Callable[[argparse.Namespace], int],
        help_text: str,
        *,
        connection: bool = True,
    ) -> argparse.ArgumentParser:
        sub = actions.add_parser(name, help=help_text)
        if connection:
            sub.add_argument("--connection", required=True, help="Connection id or name.")
        sub.set_defaults(handler=handler)
        return sub

    add_action("list", _run_list, "List the connections.", connection=False)
    adding = add_action("add", _run_add, "Connect a Guesty account.", connection=False)
    adding.add_argument("--label", required=True, help="A name for the connection.")
    adding.add_argument("--client-id", required=True, dest="client_id")
    adding.add_argument("--from-stdin", action="store_true", help="Read the secret from stdin.")
    rotating = add_action("rotate", _run_rotate, "Replace a connection's credentials.")
    rotating.add_argument("--client-id", required=True, dest="client_id")
    rotating.add_argument("--from-stdin", action="store_true", help="Read the secret from stdin.")
    add_action("test", _simple(test), "Check a connection with the stored token.")
    add_action("disable", _simple(disable), "Turn a connection off.")
    add_action("enable", _simple(enable), "Turn a connection on.")
    add_action("allow-changes", _run_allow, "Let assistants make changes.").add_argument(
        "--yes", action="store_true", help="Confirm."
    )
    add_action("stop-changes", _simple(stop_changes), "Stop assistants making changes.")
    add_action("remove", _run_remove, "Remove a connection.").add_argument(
        "--yes", action="store_true", help="Confirm."
    )
