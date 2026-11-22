from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from pmsbridge.accounts import sessions
from pmsbridge.accounts.db import Session, User
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import session, write_transaction


async def stored_sessions() -> list[Session]:
    async with session() as db:
        return list((await db.execute(select(Session))).scalars())


async def age_session(
    token: str, *, seen: timedelta = timedelta(0), created: timedelta = timedelta(0)
) -> None:
    now = datetime.now(UTC)
    async with write_transaction() as db:
        await db.execute(
            update(Session)
            .where(Session.id == sessions.hash_token(token))
            .values(
                last_seen_at=now - seen,
                created_at=now - created,
                expires_at=now - created + timedelta(days=7),
            )
        )


async def test_only_the_hash_of_the_cookie_value_is_stored(app: FastAPI, user_id: int) -> None:
    token = await sessions.create_session(user_id, "Example Browser/1.0")
    [row] = await stored_sessions()
    assert row.id == sessions.hash_token(token)
    assert token not in (row.id, row.csrf_secret, row.user_agent_hash)
    assert row.user_agent_hash is not None
    assert "Example Browser" not in row.user_agent_hash


async def test_cookie_values_are_random_and_unique(app: FastAPI, user_id: int) -> None:
    tokens = {await sessions.create_session(user_id) for _ in range(5)}
    assert len(tokens) == 5
    assert all(len(token) == 43 for token in tokens)


async def test_a_live_session_loads(app: FastAPI, user_id: int) -> None:
    token = await sessions.create_session(user_id)
    info = await sessions.load_session(token)
    assert info is not None
    assert (info.user_id, info.role) == (user_id, "staff")


async def test_unknown_and_malformed_values_do_not_load(app: FastAPI, user_id: int) -> None:
    await sessions.create_session(user_id)
    for value in (None, "", "short", "x" * 43, "a" * 44, "bad value" + "a" * 40):
        assert await sessions.load_session(value) is None


async def test_idle_timeout(app: FastAPI, user_id: int) -> None:
    token = await sessions.create_session(user_id)
    await age_session(token, seen=timedelta(hours=11, minutes=59))
    assert await sessions.load_session(token) is not None
    await age_session(token, seen=timedelta(hours=12, minutes=1))
    assert await sessions.load_session(token) is None
    assert await stored_sessions() == []


async def test_activity_extends_the_idle_timeout_but_not_the_absolute_one(
    app: FastAPI, user_id: int
) -> None:
    token = await sessions.create_session(user_id)
    await age_session(token, seen=timedelta(hours=11))
    assert await sessions.load_session(token) is not None
    [row] = await stored_sessions()
    assert datetime.now(UTC) - row.last_seen_at < timedelta(minutes=1)
    await age_session(token, seen=timedelta(minutes=5), created=timedelta(days=7, minutes=1))
    assert await sessions.load_session(token) is None


async def test_settings_change_the_lifetimes(
    app: FastAPI, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SESSION_IDLE_HOURS", "1")
    monkeypatch.setenv("SESSION_MAX_DAYS", "2")
    clear_settings_cache()
    token = await sessions.create_session(user_id)
    [row] = await stored_sessions()
    assert row.expires_at - row.created_at == timedelta(days=2)
    await age_session(token, seen=timedelta(hours=2))
    assert await sessions.load_session(token) is None


async def test_a_shorter_absolute_lifetime_applies_to_existing_sessions(
    app: FastAPI, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await sessions.create_session(user_id)
    # Created two days ago with a stored expiry five days ahead (made under the 7 day setting).
    await age_session(token, created=timedelta(days=2))
    assert await sessions.load_session(token) is not None
    monkeypatch.setenv("SESSION_MAX_DAYS", "1")
    clear_settings_cache()
    assert await sessions.load_session(token) is None
    assert await stored_sessions() == []


async def test_cleanup_uses_the_live_absolute_lifetime(
    app: FastAPI, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await sessions.create_session(user_id)
    await age_session(old, created=timedelta(days=2))
    monkeypatch.setenv("SESSION_MAX_DAYS", "1")
    clear_settings_cache()
    await sessions.create_session(user_id)
    assert len(await stored_sessions()) == 1


async def test_disabling_the_user_ends_the_session(app: FastAPI, user_id: int) -> None:
    token = await sessions.create_session(user_id)
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == user_id).values(is_active=False))
    assert await sessions.load_session(token) is None


async def test_end_session_and_end_user_sessions(app: FastAPI, user_id: int) -> None:
    first = await sessions.create_session(user_id)
    await sessions.create_session(user_id)
    await sessions.end_session(first)
    assert await sessions.load_session(first) is None
    assert len(await stored_sessions()) == 1
    assert await sessions.end_user_sessions(user_id) == 1
    assert await stored_sessions() == []


async def test_creating_a_session_cleans_up_expired_ones(app: FastAPI, user_id: int) -> None:
    stale = await sessions.create_session(user_id)
    await age_session(stale, seen=timedelta(hours=13))
    await sessions.create_session(user_id)
    assert len(await stored_sessions()) == 1
