"""User administration, run in the container as `python -m pmsbridge.admin users <action>`.

`create-admin --email <address>` makes an administrator who cannot sign in yet and prints a one-time
setup link; the person opens it, chooses a password and turns on two-factor sign-in.
Running it again for an administrator who has not finished setup prints a new link and the old
one stops working.
`reset-admin-password --email <address>` prints a one-time link for an existing administrator to
choose a new password (their two-factor sign-in stays on). Both links work once and end after 24
hours. The only thing printed to standard output is the link. Passwords and codes are never
arguments. Each action writes an audit line without secrets.

Log records (the audit lines) belong on standard error: the admin dispatcher sets that up, and
standard output is kept for the link. Nothing else is ever written to standard output.

Exit codes: 0 done, 1 refused (for example the address already has an active account), 2 bad
input, configuration or a database that cannot be used.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from pmsbridge.accounts.db import User
from pmsbridge.accounts.flows import issue_token, link_for
from pmsbridge.accounts.service import InvalidEmailError, find_user_by_email, normalise_email
from pmsbridge.core import storage
from pmsbridge.core.settings import SettingsError, get_settings
from pmsbridge.core.storage import StorageError, write_transaction

logger = logging.getLogger(__name__)

UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105  (not a hash: no password can match it)


class ActionRefusedError(Exception):
    """The action cannot be done. The message is safe to print and says what to do next."""


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


def _run(work: Callable[[str], Coroutine[object, object, str]], email: str) -> int:
    try:
        link = asyncio.run(work(email))
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
    sys.stdout.write(f"{link}\n")
    return 0


def _run_create_admin(args: argparse.Namespace) -> int:
    return _run(create_admin, str(args.email))  # pyright: ignore[reportAny]


def _run_reset_admin_password(args: argparse.Namespace) -> int:
    return _run(reset_admin_password, str(args.email))  # pyright: ignore[reportAny]


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
) -> None:
    users = subparsers.add_parser("users", help="Administrator accounts.")
    actions = users.add_subparsers(dest="action", required=True)
    create = actions.add_parser(
        "create-admin", help="Create an administrator and print a one-time setup link."
    )
    create.add_argument("--email", required=True)
    create.set_defaults(handler=_run_create_admin)
    reset = actions.add_parser(
        "reset-admin-password", help="Print a one-time link to choose a new administrator password."
    )
    reset.add_argument("--email", required=True)
    reset.set_defaults(handler=_run_reset_admin_password)
