# ruff: noqa: S105  (the passwords and tokens in these tests are made-up values)
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, update

from conftest import (
    BASE_URL,
    PASSWORD,
    RECOVERY_CODE,
    current_code,
    post_form,
    secret_from_page,
    sign_in,
)
from pmsbridge.accounts import management
from pmsbridge.accounts.db import AuthFlow, OneTimeToken, User, UserTotp
from pmsbridge.accounts.flows import FLOW_COOKIE
from pmsbridge.accounts.service import create_user, find_user_by_email
from pmsbridge.accounts.setup import LINK_INVALID
from pmsbridge.admin import users as admin_users
from pmsbridge.core.storage import session, write_transaction

NEW_PASSWORD = "an-invited-fake-passphrase"
INVITEE = "new.person@example.test"


async def rows[T](model: type[T]) -> list[T]:
    async with session() as db:
        return list((await db.execute(select(model))).scalars().all())


async def invite(email: str = INVITEE, role: str = "staff") -> str:
    """The link an administrator would share."""
    return await admin_users.invite(email, role)


async def open_link(client: httpx.AsyncClient, link: str) -> httpx.Response:
    parts = urlsplit(link)
    return await client.get(parts.path, params=parse_qs(parts.query))


async def choose_password(client: httpx.AsyncClient) -> httpx.Response:
    data = {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    return await post_form(client, "/invite", data)


async def expire_links() -> None:
    async with write_transaction() as db:
        await db.execute(
            update(OneTimeToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def test_an_invite_link_lasts_72_hours_and_only_its_hash_is_stored(app: object) -> None:
    link = await invite()
    assert link.startswith(f"{BASE_URL}/invite?token=")
    token = parse_qs(urlsplit(link).query)["token"][0]
    [row] = await rows(OneTimeToken)
    assert row.purpose == "invite"
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.expires_at - row.created_at == timedelta(hours=72)
    user = await find_user_by_email(INVITEE)
    assert user is not None
    assert (user.role, user.is_active, user.setup_pending) == ("staff", False, True)


async def test_a_person_who_has_not_accepted_cannot_sign_in(
    app: object, client: httpx.AsyncClient
) -> None:
    await invite()
    response = await sign_in(client, INVITEE, NEW_PASSWORD)
    assert response.status_code == 400
    assert "Email or password is incorrect" in response.text


async def test_the_token_leaves_the_address_and_the_link_is_not_used_up_by_opening(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await invite()
    token = parse_qs(urlsplit(link).query)["token"][0]
    for _ in range(2):
        response = await open_link(client, link)
        assert (response.status_code, response.headers["location"]) == (303, "/invite")
    assert len(await rows(OneTimeToken)) == 1
    page = await client.get("/invite")
    assert page.status_code == 200
    assert token not in page.text
    assert INVITEE in page.text


async def test_staff_invitation_from_link_to_sign_in_and_single_use(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await invite()
    await open_link(client, link)
    done = await choose_password(client)
    assert done.status_code == 200
    assert FLOW_COOKIE in "".join(done.headers.get_list("set-cookie"))  # cleared
    user = await find_user_by_email(INVITEE)
    assert user is not None
    assert (user.is_active, user.setup_pending, user.role) == (True, False, "staff")
    assert await rows(OneTimeToken) == []
    assert (await open_link(client, link)).status_code == 403  # used up
    signed = await sign_in(client, INVITEE, NEW_PASSWORD)
    assert signed.headers["location"] == "/"


async def test_an_invited_administrator_is_not_active_until_two_factor_is_confirmed(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await invite("root.two@example.test", "admin")
    await open_link(client, link)
    chosen = await choose_password(client)
    assert (chosen.status_code, chosen.headers["location"]) == (303, "/two-factor/enroll")
    user = await find_user_by_email("root.two@example.test")
    assert user is not None
    assert (user.is_active, user.setup_pending, user.role) == (False, True, "admin")
    assert (await sign_in(client, "root.two@example.test", NEW_PASSWORD)).status_code == 400

    await open_link(client, link)
    await choose_password(client)
    enroll = await client.get("/two-factor/enroll")
    secret = secret_from_page(enroll.text)
    wrong = await post_form(client, "/two-factor/enroll", {"code": "000000"})
    assert wrong.status_code == 400
    assert not (await find_user_by_email("root.two@example.test")).is_active  # pyright: ignore[reportOptionalMemberAccess]
    done = await post_form(client, "/two-factor/enroll", {"code": current_code(secret)})
    assert done.status_code == 200
    assert len(RECOVERY_CODE.findall(done.text)) == 10
    user = await find_user_by_email("root.two@example.test")
    assert user is not None
    assert (user.is_active, user.setup_pending) == (True, False)
    assert len(await rows(UserTotp)) == 1
    again = await sign_in(client, "root.two@example.test", NEW_PASSWORD)
    assert again.headers["location"] == "/login/verify"


async def test_an_expired_invite_is_refused_and_one_that_runs_out_mid_flow_cannot_finish(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await invite()
    await open_link(client, link)
    await expire_links()
    late = await choose_password(client)
    assert late.status_code == 403
    assert LINK_INVALID in late.text
    user = await find_user_by_email(INVITEE)
    assert user is not None
    assert (user.is_active, user.setup_pending) == (False, True)
    response = await open_link(client, link)
    assert response.status_code == 403
    assert LINK_INVALID in response.text


async def test_inviting_again_replaces_the_link_and_the_role_and_ends_open_flows(
    app: object, client: httpx.AsyncClient
) -> None:
    first = await invite()
    await open_link(client, first)
    second = await invite(INVITEE.upper(), "admin")
    assert first != second
    assert len(await rows(OneTimeToken)) == 1
    assert await rows(AuthFlow) == []
    assert (await client.get("/invite")).status_code == 403
    assert (await open_link(client, first)).status_code == 403
    assert (await open_link(client, second)).status_code == 303
    assert len(await rows(User)) == 1
    assert (await find_user_by_email(INVITEE)).role == "admin"  # pyright: ignore[reportOptionalMemberAccess]


async def test_revoking_an_invitation_removes_the_person_and_the_link(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await invite()
    await open_link(client, link)
    user = await find_user_by_email(INVITEE)
    assert user is not None
    await management.revoke_invite(user.id, None)
    assert await find_user_by_email(INVITEE) is None
    assert await rows(OneTimeToken) == []
    assert await rows(AuthFlow) == []
    assert (await open_link(client, link)).status_code == 403
    with pytest.raises(management.ActionRefusedError):
        await management.revoke_invite(user.id, None)


async def test_only_pending_people_can_have_an_invitation_revoked(app: object) -> None:
    active = await create_user("active@example.test", PASSWORD)
    with pytest.raises(management.ActionRefusedError, match="already set up"):
        await management.revoke_invite(active, None)
    assert await find_user_by_email("active@example.test") is not None


async def test_inviting_an_address_with_an_active_or_disabled_account_is_refused(
    app: object,
) -> None:
    active = await create_user("active@example.test", PASSWORD)
    gone = await create_user("gone@example.test", PASSWORD)
    await management.disable_user(gone, None)
    with pytest.raises(management.ActionRefusedError, match="already exists"):
        await invite("ACTIVE@example.test")
    with pytest.raises(management.ActionRefusedError, match="disabled account"):
        await invite("gone@example.test")
    with pytest.raises(management.ActionRefusedError, match="role"):
        await invite("someone@example.test", "owner")
    assert await rows(OneTimeToken) == []
    assert (await find_user_by_email("active@example.test")).id == active  # pyright: ignore[reportOptionalMemberAccess]


async def test_an_invite_link_does_not_work_on_the_other_pages(
    app: object, client: httpx.AsyncClient
) -> None:
    token = parse_qs(urlsplit(await invite()).query)["token"][0]
    for path in ("/setup", "/reset"):
        assert (await client.get(path, params={"token": token})).status_code == 403
