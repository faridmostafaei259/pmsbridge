"""Creating and looking up users. The sign-in flow and later admin commands build on this."""

import re
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pmsbridge.accounts.db import ROLES, User
from pmsbridge.accounts.passwords import hash_password
from pmsbridge.core.storage import session, write_transaction

MAX_EMAIL_LENGTH: Final = 254
_EMAIL_SHAPE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class InvalidEmailError(ValueError):
    """The text is not a usable e-mail address."""


class EmailTakenError(ValueError):
    """A user with this e-mail address already exists."""


def normalise_email(value: str) -> str:
    """Trim and lower-case an e-mail address, or raise `InvalidEmailError`."""
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_SHAPE.fullmatch(email):
        raise InvalidEmailError("Enter a valid e-mail address.")
    return email


async def create_user(email: str, password: str, role: str = "staff") -> int:
    """Create an active user and return its id. Raises on a bad e-mail, weak password or repeat."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    normalised = normalise_email(email)
    password_hash = await hash_password(password)
    try:
        async with write_transaction() as db:
            user = User(
                email=normalised,
                password_hash=password_hash,
                role=role,
                is_active=True,
                created_at=datetime.now(UTC),
            )
            db.add(user)
            await db.flush()
            return user.id
    except IntegrityError:
        raise EmailTakenError("A user with this e-mail address already exists.") from None


async def find_user_by_email(email: str) -> User | None:
    async with session() as db:
        return (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
