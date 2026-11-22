"""User administration, run in the container as `python -m pmsbridge.admin users <action>`.

Actions:

- `create-admin --email <address>` makes an administrator who cannot sign in yet and prints a
  one-time setup link; the person opens it, chooses a password and turns on two-factor sign-in.
  Running it again for an administrator who has not finished setup prints a new link and the old
  one stops working.
- `reset-admin-password --email <address>` prints a one-time link for an existing administrator
  to choose a new password (their two-factor sign-in stays on).
- `list` prints every account: email, role, status (active, disabled or pending), whether
  two-factor sign-in is on, and the last sign-in.
- `invite --email <address> --role admin|staff` prints a one-time invitation link (72 hours). An
  address that already has an active or disabled account is refused.
- `disable --email <address>` and `enable --email <address>`. Disabling signs the person out
  everywhere and is refused for the last active administrator.
- `reset-2fa --email <address>` deletes the person's authenticator and recovery codes and signs
  them out. This is the way back in for an administrator who lost their phone and recovery codes
  (an administrator enrolls again at the next sign-in). It is allowed for the last administrator,
  because the command runs with server access.

Setup, reset and invitation links work once. Setup and reset links end after 24 hours, invitation
links after 72. Passwords and codes are never arguments. Each action writes an audit line without
secrets or addresses (the acting user is `cli`).

Log records (the audit lines) belong on standard error: the admin dispatcher sets that up, and
standard output is kept for the command's result: a link, a short confirmation, or the list.

Exit codes: 0 done, 1 refused (for example the address already has an active account), 2 bad
input, configuration or a database that cannot be used.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from pmsbridge.accounts import management
from pmsbridge.accounts.db import AuthFlow, OneTimeToken, User
from pmsbridge.accounts.flows import issue_token, link_for
from pmsbridge.accounts.management import ActionRefusedError
from pmsbridge.accounts.service import InvalidEmailError, find_user_by_email, normalise_email
from pmsbridge.core import storage
from pmsbridge.core.settings import SettingsError, get_settings
from pmsbridge.core.storage import StorageError, write_transaction

logger = logging.getLogger(__name__)

UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105  (not a hash: no password can match it)


async def create_admin(email: str) -> str:
    """Create (or re-invite) a pending administrator and return the setup link."""
    storage.import_model_modules(storage.MODEL_MODULES)
    base_url = get_settings().public_base_url  # before any change, so a settings error leaves none
    address = normalise_email(email)
    existing = await find_user_by_email(address)
    if existing is None:
        async with write_transaction() as db:
            user = User(
                email=address,
                password_hash=UNUSABLE_PASSWORD_HASH,
                role="admin",
                is_active=False,
                setup_pending=True,
                created_at=datetime.now(UTC),
            )
            db.add(user)
            await db.flush()
            user_id = user.id
    elif existing.setup_pending and existing.role == "admin":
        user_id = existing.id
        # Any earlier link or half-finished flow for this person (an invitation, for example)
        # stops working: only the link printed now is valid.
        async with write_transaction() as db:
            await db.execute(delete(OneTimeToken).where(OneTimeToken.user_id == user_id))
            await db.execute(delete(AuthFlow).where(AuthFlow.user_id == user_id))
    else:
        raise ActionRefusedError(
            "A user with this email address already exists. To give an administrator a new "
            "password, use reset-admin-password."
        )
    token = await issue_token(user_id, "setup")
    logger.info("admin users create-admin", extra={"user_id": user_id})
    return link_for("/setup", token, base_url)


async def reset_admin_password(email: str) -> str:
    """Return a one-time link for an existing, active administrator to choose a new password."""
    storage.import_model_modules(storage.MODEL_MODULES)
    base_url = get_settings().public_base_url
    user = await find_user_by_email(normalise_email(email))
    if user is None or user.role != "admin":
        raise ActionRefusedError("There is no administrator with this email address.")
    if user.setup_pending:
        raise ActionRefusedError("This administrator has not finished setup. Use create-admin.")
    if not user.is_active:
        raise ActionRefusedError("This account is disabled. Enable it first.")
    token = await issue_token(user.id, "reset")
    logger.info("admin users reset-admin-password", extra={"user_id": user.id})
    return link_for("/reset", token, base_url)


def run_action(work: Callable[[], Coroutine[object, object, str]]) -> int:
    """Run an action. Its text goes to standard output; every failure is a short message."""
    try:
        output = asyncio.run(work())
    except InvalidEmailError as problem:
        sys.stderr.write(f"Error: {problem}\n")
        return 2
    except ActionRefusedError as problem:
        sys.stderr.write(f"Error: {problem}\n")
        return 1
    except SettingsError as problem:
        sys.stderr.write(f"Error: {problem}\nNext step: check the settings in .env.\n")
        return 2
    except (SQLAlchemyError, StorageError) as problem:
        # Not printed: database errors can quote statements and values.
        logger.error("admin users action failed: %s", type(problem).__name__)
        sys.stderr.write(
            "Error: the database could not be used.\n"
            "Next step: check that the application has started (it prepares the database), "
            "then try again.\n"
        )
        return 2
    sys.stdout.write(f"{output}\n")
    return 0


async def list_users() -> str:
    storage.import_model_modules(storage.MODEL_MODULES)
    listed = await management.list_users()
    rows = listed.rows
    if not rows:
        return "There are no users yet."
    table = [("EMAIL", "ROLE", "STATUS", "TWO-FACTOR", "LAST SIGN-IN")] + [
        (
            row.email,
            row.role,
            row.status,
            "on" if row.two_factor else "off",
            row.last_login_at.strftime("%Y-%m-%d %H:%M UTC") if row.last_login_at else "never",
        )
        for row in rows
    ]
    widths = [max(len(line[column]) for line in table) for column in range(5)]
    text = "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in table
    )
    if listed.truncated:
        text += f"\nShowing the first {management.MAX_LISTED} users only."
    return text


async def invite(email: str, role: str) -> str:
    storage.import_model_modules(storage.MODEL_MODULES)
    base_url = get_settings().public_base_url
    token = await management.invite_user(email, role, None)
    return link_for("/invite", token, base_url)


async def _target(email: str) -> int:
    storage.import_model_modules(storage.MODEL_MODULES)
    return (await management.user_by_email(email)).id


async def disable(email: str) -> str:
    await management.disable_user(await _target(email), None)
    return "The account is disabled and the person is signed out everywhere."


async def enable(email: str) -> str:
    await management.enable_user(await _target(email), None)
    return "The account is active again."


async def reset_two_factor(email: str) -> str:
    was_admin = await management.reset_two_factor(await _target(email), None)
    text = "Two-factor sign-in was reset and the person is signed out everywhere."
    if was_admin:
        return f"{text} They must set it up again at their next sign-in."
    return text


def _args_email(args: argparse.Namespace) -> str:
    return str(args.email)  # pyright: ignore[reportAny]


def _run_create_admin(args: argparse.Namespace) -> int:
    return run_action(lambda: create_admin(_args_email(args)))


def _run_reset_admin_password(args: argparse.Namespace) -> int:
    return run_action(lambda: reset_admin_password(_args_email(args)))


def _run_list(_args: argparse.Namespace) -> int:
    return run_action(list_users)


def _run_invite(args: argparse.Namespace) -> int:
    return run_action(lambda: invite(_args_email(args), str(args.role)))  # pyright: ignore[reportAny]


def _run_disable(args: argparse.Namespace) -> int:
    return run_action(lambda: disable(_args_email(args)))


def _run_enable(args: argparse.Namespace) -> int:
    return run_action(lambda: enable(_args_email(args)))


def _run_reset_two_factor(args: argparse.Namespace) -> int:
    return run_action(lambda: reset_two_factor(_args_email(args)))


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
) -> None:
    users = subparsers.add_parser("users", help="User accounts.")
    actions = users.add_subparsers(dest="action", required=True)

    def add(
        name: str,
        handler: Callable[[argparse.Namespace], int],
        help_text: str,
        *,
        email: bool = True,
    ) -> argparse.ArgumentParser:
        parser = actions.add_parser(name, help=help_text)
        if email:
            parser.add_argument("--email", required=True)
        parser.set_defaults(handler=handler)
        return parser

    add(
        "create-admin",
        _run_create_admin,
        "Create an administrator and print a one-time setup link.",
    )
    add(
        "reset-admin-password",
        _run_reset_admin_password,
        "Print a one-time link to choose a new administrator password.",
    )
    add("list", _run_list, "List the accounts.", email=False)
    add("invite", _run_invite, "Print a one-time invitation link.").add_argument(
        "--role", choices=("admin", "staff"), required=True
    )
    add("disable", _run_disable, "Disable an account and sign the person out everywhere.")
    add("enable", _run_enable, "Turn a disabled account on again.")
    add("reset-2fa", _run_reset_two_factor, "Reset two-factor sign-in (also for an administrator).")
