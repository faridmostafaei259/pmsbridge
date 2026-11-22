"""AI assistant access, run in the container as `python -m pmsbridge.admin oauth <action>`.

`revoke-ai-access [--user <email>] [--client <client_id>] [--all] [--yes]` cancels assistant
approvals, which ends their access and refresh tokens at once:

- `--user` cancels every approval one person gave;
- `--client` cancels every approval given to one assistant (its client id is on the administrator
  assistants page);
- `--user` and `--client` together cancel only that person's approvals for that assistant;
- `--all` cancels every approval of everyone and needs `--yes`, because every assistant then has
  to be approved again. It cannot be combined with the other selectors.

At least one selector is required. The command prints counts only (approvals, people, assistants).
It writes one audit line (the acting user is `cli`) with the person's numeric id and the client id
when they were given, and without email addresses or tokens. There are no
signing keys to rotate: tokens are random values checked against the database (see ADR 0011).

Exit codes: 0 done (also when nothing matched), 1 refused (no such person or assistant), 2 bad
input, configuration or a database that cannot be used.
"""

import argparse
import logging
import re
import sys

from sqlalchemy import select

from pmsbridge.accounts import management
from pmsbridge.admin.users import run_action
from pmsbridge.core import storage
from pmsbridge.core.storage import session
from pmsbridge.oauth import grants
from pmsbridge.oauth.db import OAuthClient

logger = logging.getLogger(__name__)

_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")


async def revoke_ai_access(*, email: str | None, client_id: str | None) -> str:
    """Revoke the matching approvals (all of them with no selector) and return the counts."""
    storage.import_model_modules(storage.MODEL_MODULES)
    user_id = None if email is None else (await management.user_by_email(email)).id
    if client_id is not None:
        async with session() as db:
            known = await db.scalar(
                select(OAuthClient.client_id).where(OAuthClient.client_id == client_id)
            )
        if known is None:
            raise management.ActionRefusedError("There is no assistant with this client id.")
    done = await grants.revoke_grants(user_id=user_id, client_id=client_id)
    logger.info(
        "admin oauth revoke-ai-access",
        extra={
            "acting_user": "cli",
            "selector": "all" if user_id is None and client_id is None else "filtered",
            "target_user_id": user_id,
            "client_id": client_id,
            "grants": done.grants,
            "people": done.people,
            "assistants": done.assistants,
        },
    )
    return (
        f"Approvals cancelled: {done.grants}\n"
        f"People affected: {done.people}\n"
        f"Assistants affected: {done.assistants}"
    )


def _usage_error(message: str) -> int:
    sys.stderr.write(f"Error: {message}\n")
    return 2


def _run_revoke(args: argparse.Namespace) -> int:
    email = None if args.user is None else str(args.user)  # pyright: ignore[reportAny]
    client_id = None if args.client is None else str(args.client)  # pyright: ignore[reportAny]
    everyone = bool(args.all)  # pyright: ignore[reportAny]
    if everyone and (email is not None or client_id is not None):
        return _usage_error("--all cannot be combined with --user or --client.")
    if not everyone and email is None and client_id is None:
        return _usage_error("Choose who to cancel: --user, --client or --all.")
    if everyone and not args.yes:  # pyright: ignore[reportAny]
        return _usage_error("--all cancels every approval of everyone. Add --yes to confirm.")
    if client_id is not None and not _CLIENT_ID.fullmatch(client_id):
        return _usage_error("The client id is not valid.")
    return run_action(lambda: revoke_ai_access(email=email, client_id=client_id))


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
) -> None:
    oauth = subparsers.add_parser("oauth", help="AI assistant access.")
    actions = oauth.add_subparsers(dest="action", required=True)
    revoke = actions.add_parser(
        "revoke-ai-access", help="Cancel assistant approvals (access and refresh tokens end)."
    )
    revoke.add_argument("--user", help="Cancel the approvals one person gave (email address).")
    revoke.add_argument("--client", help="Cancel the approvals given to one assistant (client id).")
    revoke.add_argument("--all", action="store_true", help="Cancel every approval of everyone.")
    revoke.add_argument("--yes", action="store_true", help="Confirm --all.")
    revoke.set_defaults(handler=_run_revoke)
