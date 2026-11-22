"""Managing other people's accounts: invite, disable, enable, reset two-factor, change role.

The dashboard users page and the `users` admin commands both call these functions, so the safety
rules are written once:

- a person cannot disable, demote or reset the two-factor sign-in of their own account;
- the last active administrator cannot be disabled or demoted;
- disabling ends every session and half-finished sign-in of the person;
- resetting two-factor sign-in deletes the secret and the recovery codes and ends their sessions
  (an administrator then has to enroll again at the next sign-in);
- making someone an administrator ends their sessions, so the two-factor rule applies at once.

`acting_user_id` is the signed-in administrator, or None for the command line (which runs with
server access and so may also reset the two-factor sign-in of the last administrator).
Every action writes one audit line with the acting user id (or `cli`), the target user id and the
action. Addresses, links and tokens are never logged.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pmsbridge.accounts.db import (
    ROLES,
    AuthFlow,
    OneTimeToken,
    RecoveryCode,
    Session,
    User,
    UserTotp,
)
from pmsbridge.accounts.flows import issue_token_in
from pmsbridge.accounts.service import normalise_email
from pmsbridge.accounts.twofactor import remove_two_factor
from pmsbridge.core.storage import session, write_transaction

logger = logging.getLogger("pmsbridge.accounts")

Status = Literal["active", "disabled", "pending"]
MAX_LISTED = 500


class ActionRefusedError(Exception):
    """The action cannot be done. The message is safe to show and says what to do next."""


@dataclass(frozen=True)
class UserRow:
    id: int
    email: str
    role: str
    status: Status
    two_factor: bool
    last_login_at: datetime | None
    link_expires_at: datetime | None  # the live invite or setup link of a pending person


@dataclass(frozen=True)
class UserList:
    rows: list[UserRow]
    truncated: bool  # more people exist than the first MAX_LISTED shown


def audit(acting_user_id: int | None, target_user_id: int, action: str) -> None:
    logger.info(
        "user administration",
        extra={
            "acting_user": "cli" if acting_user_id is None else acting_user_id,
            "target_user_id": target_user_id,
            "action": action,
        },
    )


def status_of(user: User) -> Status:
    if user.setup_pending:
        return "pending"
    return "active" if user.is_active else "disabled"


async def list_users() -> UserList:
    """The first 500 people by id, with two-factor state and any live link."""
    now = datetime.now(UTC)
    async with session() as db:
        found = (await db.execute(select(User).order_by(User.id).limit(MAX_LISTED + 1))).scalars()
        users = list(found.all())
        truncated = len(users) > MAX_LISTED
        users = users[:MAX_LISTED]
        ids = [user.id for user in users]
        enrolled = set(
            (await db.execute(select(UserTotp.user_id).where(UserTotp.user_id.in_(ids))))
            .scalars()
            .all()
        )
        links = {
            row.user_id: row.expires_at
            for row in (
                await db.execute(
                    select(OneTimeToken).where(
                        OneTimeToken.expires_at > now, OneTimeToken.user_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
            if row.purpose in ("invite", "setup")
        }
    rows = [
        UserRow(
            u.id,
            u.email,
            u.role,
            status_of(u),
            u.id in enrolled,
            u.last_login_at,
            links.get(u.id) if u.setup_pending else None,
        )
        for u in users
    ]
    return UserList(rows, truncated)


async def email_of(user_id: int) -> str | None:
    """The address of the person with this id, for confirmation pages."""
    async with session() as db:
        user = await db.get(User, user_id)
    return None if user is None else user.email


async def user_by_email(email: str) -> User:
    """The user with this address, or a refusal."""
    async with session() as db:
        found = (
            await db.execute(select(User).where(User.email == normalise_email(email)))
        ).scalar_one_or_none()
    if found is None:
        raise ActionRefusedError("There is no user with this email address.")
    return found


async def _get(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise ActionRefusedError("That user no longer exists. Reload the page.")
    return user


async def _other_active_admins(db: AsyncSession, user_id: int) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.role == "admin",
            User.is_active.is_(True),
            User.setup_pending.is_(False),
            User.id != user_id,
        )
    )
    return int(count or 0)


async def _end_access(db: AsyncSession, user_id: int) -> None:
    """End every session and half-finished sign-in of the person."""
    await db.execute(delete(Session).where(Session.user_id == user_id))
    await db.execute(delete(AuthFlow).where(AuthFlow.user_id == user_id))


async def _require_acting_admin(db: AsyncSession, acting_user_id: int | None) -> None:
    """The signed-in administrator must still be an active administrator when the change is made.

    Their session was checked at the start of the request; they may have been disabled or
    demoted since (for example by another administrator at the same moment). None is the
    command line, which runs with server access.
    """
    if acting_user_id is None:
        return
    actor = await db.get(User, acting_user_id)
    if actor is None or actor.role != "admin" or not actor.is_active or actor.setup_pending:
        raise ActionRefusedError("Your account can no longer do this. Sign in again.")


def _refuse_self(acting_user_id: int | None, target_id: int, what: str) -> None:
    if acting_user_id is not None and acting_user_id == target_id:
        raise ActionRefusedError(
            f"You cannot {what} your own account. Ask another administrator to do it."
        )


async def invite_user(email: str, role: str, acting_user_id: int | None) -> str:
    """Create (or re-invite) a pending person and return the one-time token for the link.

    An address that already has an active or disabled account is refused. Inviting a pending
    address again replaces the old link and sets the new role.
    """
    if role not in ROLES:
        raise ActionRefusedError("Choose the role admin or staff.")
    address = normalise_email(email)
    try:
        async with write_transaction() as db:
            await _require_acting_admin(db, acting_user_id)
            user = (
                await db.execute(select(User).where(User.email == address))
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    email=address,
                    password_hash="!",  # noqa: S106  (not a hash: no password can match it)
                    role=role,
                    is_active=False,
                    setup_pending=True,
                    created_at=datetime.now(UTC),
                )
                db.add(user)
                await db.flush()
            elif user.setup_pending:
                user.role = role
                await db.execute(delete(OneTimeToken).where(OneTimeToken.user_id == user.id))
                await db.execute(delete(AuthFlow).where(AuthFlow.user_id == user.id))
            elif user.is_active:
                raise ActionRefusedError("A user with this email address already exists.")
            else:
                raise ActionRefusedError(
                    "A disabled account with this email address already exists. Turn it on again "
                    "from the users list."
                )
            user_id = user.id
            # In the same transaction as the person, so a cancellation at the same moment cannot
            # leave a link for a person who no longer exists.
            token = await issue_token_in(db, user_id, "invite")
    except IntegrityError:
        raise ActionRefusedError("A user with this email address already exists.") from None
    audit(acting_user_id, user_id, f"invite as {role}")
    return token


async def revoke_invite(user_id: int, acting_user_id: int | None) -> None:
    """Cancel an invitation: the person who has not finished setup is removed, the link dies."""
    async with write_transaction() as db:
        await _require_acting_admin(db, acting_user_id)
        user = await _get(db, user_id)
        if not user.setup_pending:
            raise ActionRefusedError("This person has already set up their account.")
        await db.execute(delete(User).where(User.id == user_id))
    audit(acting_user_id, user_id, "revoke invite")


async def disable_user(user_id: int, acting_user_id: int | None) -> None:
    _refuse_self(acting_user_id, user_id, "disable")
    async with write_transaction() as db:
        await _require_acting_admin(db, acting_user_id)
        user = await _get(db, user_id)
        if user.setup_pending:
            raise ActionRefusedError("This person has not finished setup. Revoke the invitation.")
        if not user.is_active:
            raise ActionRefusedError("This account is already disabled.")
        if user.role == "admin" and await _other_active_admins(db, user_id) == 0:
            raise ActionRefusedError(
                "This is the last active administrator. Make another person an administrator first."
            )
        user.is_active = False
        await _end_access(db, user_id)
        await db.execute(delete(OneTimeToken).where(OneTimeToken.user_id == user_id))
    audit(acting_user_id, user_id, "disable")


async def enable_user(user_id: int, acting_user_id: int | None) -> None:
    async with write_transaction() as db:
        await _require_acting_admin(db, acting_user_id)
        user = await _get(db, user_id)
        if user.setup_pending:
            raise ActionRefusedError("This person has not finished setup. Revoke the invitation.")
        if user.is_active:
            raise ActionRefusedError("This account is already active.")
        user.is_active = True
    audit(acting_user_id, user_id, "enable")


async def reset_two_factor(user_id: int, acting_user_id: int | None) -> bool:
    """Delete the person's authenticator and recovery codes and end their sessions.

    Returns True when the person is an administrator, who must enroll again at the next sign-in.
    """
    _refuse_self(acting_user_id, user_id, "reset the two-factor sign-in of")
    async with write_transaction() as db:
        await _require_acting_admin(db, acting_user_id)
        user = await _get(db, user_id)
        if user.setup_pending:
            raise ActionRefusedError("This person has not finished setup.")
        has_secret = await db.get(UserTotp, user_id) is not None
        has_codes = (
            await db.scalar(
                select(func.count())
                .select_from(RecoveryCode)
                .where(RecoveryCode.user_id == user_id)
            )
        ) or 0
        if not has_secret and not has_codes:
            raise ActionRefusedError("Two-factor sign-in is not on for this account.")
        await remove_two_factor(db, user_id)
        await _end_access(db, user_id)
        is_admin = user.role == "admin"
    audit(acting_user_id, user_id, "reset two-factor")
    return is_admin


async def change_role(user_id: int, role: str, acting_user_id: int | None) -> None:
    """Make the person an administrator or staff. Promotion ends their sessions."""
    if role not in ROLES:
        raise ActionRefusedError("Choose the role admin or staff.")
    _refuse_self(acting_user_id, user_id, "change the role of")
    async with write_transaction() as db:
        await _require_acting_admin(db, acting_user_id)
        user = await _get(db, user_id)
        if user.setup_pending:
            raise ActionRefusedError("This person has not finished setup. Invite them again.")
        if user.role == role:
            raise ActionRefusedError(f"This person is already {role}.")
        if user.role == "admin" and user.is_active and await _other_active_admins(db, user_id) == 0:
            raise ActionRefusedError(
                "This is the last active administrator. Make another person an administrator first."
            )
        user.role = role
        if role == "admin":
            await _end_access(db, user_id)
    audit(acting_user_id, user_id, f"role {role}")
