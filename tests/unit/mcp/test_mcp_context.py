"""ToolContext and connection resolution, and the list_connections tool."""

import httpx
import pytest
from fastapi import FastAPI
from mcp_helpers import call_tool, mint_token, seed_connection, seed_user
from sqlalchemy import update

from pmsbridge.core.storage import write_transaction
from pmsbridge.mcp.context import (
    ToolContext,
    ToolRefusedError,
    build_context,
    resolve_connection,
)
from pmsbridge.oauth.db import OAuthGrant
from pmsbridge.oauth.tokens import AccessPrincipal, validate_access_token
from pmsbridge.pms.base import Provider


async def context_for(token: str) -> ToolContext:
    principal = await validate_access_token(token)
    assert principal is not None
    return await build_context(principal)


async def test_the_context_comes_from_the_principal(app: FastAPI) -> None:
    user = await seed_user()
    one = await seed_connection("One")
    token, grant = await mint_token(user, connections=(one, "gone"), client_key="assistant-7")
    ctx = await context_for(token)
    assert (ctx.user_id, ctx.client_id, ctx.grant_id) == (user, "assistant-7", grant)
    assert ctx.allowed_connection_ids == frozenset({one, "gone"})


async def test_all_connections_covers_every_connection_that_exists_now(app: FastAPI) -> None:
    user = await seed_user()
    first = await seed_connection("First")
    token, _ = await mint_token(user, all_connections=True)
    assert (await context_for(token)).allowed_connection_ids == frozenset({first})
    second = await seed_connection("Second")
    assert (await context_for(token)).allowed_connection_ids == frozenset({first, second})


async def test_an_approval_with_no_connections_allows_none(app: FastAPI) -> None:
    user = await seed_user()
    await seed_connection("First")
    token, _ = await mint_token(user)
    ctx = await context_for(token)
    assert ctx.allowed_connection_ids == frozenset()
    with pytest.raises(ToolRefusedError, match="No connections are available") as problem:
        await resolve_connection(ctx, None)
    assert problem.value.code == "no_connections"


async def test_the_only_allowed_connection_is_used_when_none_is_named(app: FastAPI) -> None:
    user = await seed_user()
    one = await seed_connection("Only one")
    await seed_connection("Not allowed")
    token, _ = await mint_token(user, connections=(one,))
    ctx = await context_for(token)
    for omitted in (None, "", "   "):
        ref = await resolve_connection(ctx, omitted)
        assert (ref.connection_id, ref.label, ref.provider) == (one, "Only one", Provider.GUESTY)


async def test_a_connection_can_be_named_by_id_or_label(app: FastAPI) -> None:
    user = await seed_user()
    one = await seed_connection("Beach houses")
    two = await seed_connection("City flats", provider="hostaway")
    token, _ = await mint_token(user, connections=(one, two))
    ctx = await context_for(token)
    assert (await resolve_connection(ctx, one)).label == "Beach houses"
    assert (await resolve_connection(ctx, "City flats")).connection_id == two
    assert (await resolve_connection(ctx, " city FLATS ")).connection_id == two
    assert (await resolve_connection(ctx, "City flats")).provider is Provider.HOSTAWAY


async def test_several_connections_need_a_choice_listing_the_labels(app: FastAPI) -> None:
    user = await seed_user()
    ids = (await seed_connection("Beta"), await seed_connection("Alpha"))
    token, _ = await mint_token(user, connections=ids)
    ctx = await context_for(token)
    with pytest.raises(ToolRefusedError) as problem:
        await resolve_connection(ctx, None)
    assert '"Alpha", "Beta"' in str(problem.value) and problem.value.code == "connection_required"


async def test_an_ambiguous_label_is_not_guessed(app: FastAPI) -> None:
    user = await seed_user()
    ids = (await seed_connection("North"), await seed_connection("north"))
    token, _ = await mint_token(user, connections=ids)
    ctx = await context_for(token)
    assert (await resolve_connection(ctx, "North")).label == "North"
    with pytest.raises(ToolRefusedError):
        await resolve_connection(ctx, "NORTH")


async def test_a_connection_that_is_not_allowed_looks_like_one_that_does_not_exist(
    app: FastAPI,
) -> None:
    user = await seed_user()
    allowed = await seed_connection("Allowed")
    secret = await seed_connection("Secret company account")
    token, _ = await mint_token(user, connections=(allowed,))
    ctx = await context_for(token)
    answers: list[str] = []
    for wanted in (secret, "Secret company account", "secret company account", "no-such-id"):
        with pytest.raises(ToolRefusedError) as problem:
            await resolve_connection(ctx, wanted)
        assert problem.value.code == "unknown_connection"
        answers.append(str(problem.value))
    assert len(set(answers)) == 1  # the text does not depend on what was asked for
    assert "Secret" not in answers[0] and secret not in answers[0]
    assert '"Allowed"' in answers[0]


async def test_a_removed_connection_is_not_resolved(app: FastAPI) -> None:
    user = await seed_user()
    token, _ = await mint_token(user, connections=("0" * 32,))
    ctx = await context_for(token)
    with pytest.raises(ToolRefusedError, match="No connections are available"):
        await resolve_connection(ctx, None)


async def test_the_context_is_frozen() -> None:
    ctx = ToolContext(1, "c", 2, frozenset())
    with pytest.raises(AttributeError):
        ctx.user_id = 5  # type: ignore[misc]


async def test_list_connections_shows_only_allowed_connections(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    one = await seed_connection("Beach houses", writes_enabled=True)
    two = await seed_connection("Broken", status="needs_attention")
    await seed_connection("Hidden")
    token, _ = await mint_token(user, connections=(two, one))
    result = await call_tool(client, token, "list_connections")
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "connections": [
            {
                "id": one,
                "label": "Beach houses",
                "provider": "guesty",
                "status": "active",
                "writes_enabled": True,
            },
            {
                "id": two,
                "label": "Broken",
                "provider": "guesty",
                "status": "needs_attention",
                "writes_enabled": False,
            },
        ]
    }


async def test_list_connections_shows_nothing_for_an_approval_with_none(
    client: httpx.AsyncClient,
) -> None:
    user = await seed_user()
    await seed_connection("Hidden")
    token, _ = await mint_token(user)
    assert (await call_tool(client, token, "list_connections"))["structuredContent"] == {
        "connections": []
    }


async def test_list_connections_follows_the_grant_as_it_changes(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    one = await seed_connection("One")
    token, grant = await mint_token(user)
    assert (await call_tool(client, token, "list_connections"))["structuredContent"] == {
        "connections": []
    }
    async with write_transaction() as db:
        await db.execute(
            update(OAuthGrant).where(OAuthGrant.id == grant).values(allowed_connection_ids=[one])
        )
    shown = (await call_tool(client, token, "list_connections"))["structuredContent"]
    assert [item["label"] for item in shown["connections"]] == ["One"]  # type: ignore[index]
    async with write_transaction() as db:
        await db.execute(
            update(OAuthGrant).where(OAuthGrant.id == grant).values(all_connections=True)
        )
    await seed_connection("Later")
    shown = (await call_tool(client, token, "list_connections"))["structuredContent"]
    assert [item["label"] for item in shown["connections"]] == ["Later", "One"]  # type: ignore[index]


async def test_list_connections_exposes_nothing_else(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    one = await seed_connection("One")
    token, _ = await mint_token(user, connections=(one,))
    shown = (await call_tool(client, token, "list_connections"))["structuredContent"]
    assert set(shown["connections"][0]) == {"id", "label", "provider", "status", "writes_enabled"}  # type: ignore[index]


def test_principal_fields_are_what_the_context_needs() -> None:
    assert {"user_id", "client_id", "grant_id", "all_connections", "allowed_connection_ids"} <= set(
        AccessPrincipal.__dataclass_fields__
    )
