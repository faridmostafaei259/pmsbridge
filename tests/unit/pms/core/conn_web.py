"""A small browser for the connection page tests: sign in, confirm, post forms with CSRF."""

import re
from collections.abc import Callable
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import update

from pmsbridge.accounts import totp, twofactor
from pmsbridge.accounts.db import UserTotp
from pmsbridge.accounts.service import create_user
from pmsbridge.core.storage import write_transaction

BASE_URL = "https://pms.example.test"
ADMIN = ("admin@example.test", "fake admin passphrase")
STAFF = ("staff@example.test", "fake staff passphrase")
ADMIN_SECRET = bytes(range(20))
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


class Site:
    """One browser. `post` loads a page for its CSRF token first, as a real browser would."""

    def __init__(self, app: FastAPI, address: str) -> None:
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app, client=(address, 40000)), base_url=BASE_URL
        )
        self.user_id = 0
        self.secret: bytes | None = None

    async def token(self, page: str = "/connections") -> str:
        match = CSRF_FIELD.search((await self.client.get(page)).text)
        assert match, "no CSRF field"
        return match.group(1)

    async def post(
        self,
        path: str,
        data: dict[str, str],
        *,
        page: str = "/connections",
        origin: str | None = BASE_URL,
        csrf: bool = True,
    ) -> httpx.Response:
        fields = dict(data)
        if csrf:
            fields["csrf_token"] = await self.token(page)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if origin is not None:
            headers["Origin"] = origin
        return await self.client.post(path, content=urlencode(fields), headers=headers)

    async def confirm(self, password: str) -> None:
        """Confirm the password and a code, so that sensitive actions are accepted for a while."""
        assert self.secret is not None
        async with write_transaction() as db:
            await db.execute(
                update(UserTotp).where(UserTotp.user_id == self.user_id).values(last_step=0)
            )
        reply = await self.post(
            "/connections/add",
            {
                "reauth": "1",
                "password": password,
                "code": totp.code_for(self.secret, totp.current_step()),
                "label": "x",
                "client_id": "y",
            },
        )
        assert reply.status_code == 200, reply.text


Browser = Callable[[str], Site]


async def sign_in(site: Site, email: str, password: str, secret: bytes | None = None) -> None:
    page = await site.client.get("/login")
    token = CSRF_FIELD.search(page.text)
    assert token
    response = await site.client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 303
    if secret is not None:
        again = CSRF_FIELD.search((await site.client.get("/login/verify")).text)
        assert again
        verified = await site.client.post(
            "/login/verify",
            data={
                "code": totp.code_for(secret, totp.current_step()),
                "csrf_token": again.group(1),
            },
            headers={"Origin": BASE_URL},
        )
        assert verified.status_code == 303


async def make_admin(site: Site) -> None:
    site.user_id = await create_user(*ADMIN, role="admin")
    async with write_transaction() as db:
        twofactor.add_totp(db, site.user_id, ADMIN_SECRET, 0)
    site.secret = ADMIN_SECRET
    await sign_in(site, *ADMIN, secret=ADMIN_SECRET)


async def make_staff(site: Site) -> None:
    site.user_id = await create_user(*STAFF, role="staff")
    await sign_in(site, *STAFF)
