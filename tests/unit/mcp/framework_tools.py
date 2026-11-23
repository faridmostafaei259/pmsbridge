"""Fake tools and providers for the framework tests."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from pmsbridge.mcp.framework import ToolCall, pms_tool
from pmsbridge.pms.base import (
    Capability,
    ConnectionRef,
    PMSAuthError,
    PMSNotFoundError,
    PMSPermissionError,
    PMSProvider,
    PMSRateLimitedError,
    PMSTokenBudgetError,
    PMSUpstreamError,
    PMSValidationError,
    Provider,
    ReservationsAPI,
    remaining_seconds,
)
from pmsbridge.pms.testing import FakeProvider

SECRET_TEXT = "raw-pms-body-with-guest-name-Jane-Doe-and-token-abc123"  # noqa: S105

PMS_ERRORS: dict[str, Exception] = {
    "auth": PMSAuthError("Nothing was changed. Try the request again."),
    "permission": PMSPermissionError("The PMS account may not do this."),
    "not_found": PMSNotFoundError("Listing not found."),
    "rate": PMSRateLimitedError("The PMS is busy.", 7.2),
    "budget": PMSTokenBudgetError("The token limit was reached."),
    "validation": PMSValidationError("Invalid pagination cursor."),
    "upstream": PMSUpstreamError("The PMS did not respond."),
    "long": PMSUpstreamError("x " * 800),
}


class WriterProvider(FakeProvider):
    """A fake provider that also supports adding reservation notes."""

    def __init__(self, label: str, provider: Provider, connection_id: str) -> None:
        super().__init__([], label)
        self._connection = ConnectionRef(connection_id, provider, label)

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.LISTINGS_READ, Capability.RESERVATIONS_ADD_NOTE})

    @property
    def reservations(self) -> ReservationsAPI | None:
        return object()  # type: ignore[return-value]  # a stand-in; tools never call it


@dataclass
class Spy:
    """What the fake tools saw, and what happened to the providers they used."""

    calls: list[dict[str, Any]] = field(default_factory=lambda: [])
    closed: int = 0


def add_tools(mcp: MCPServer, spy: Spy) -> None:
    @pms_tool(mcp, name="t_read", capability=Capability.LISTINGS_READ, api=lambda p: p.listings)
    async def t_read(call: ToolCall[Any], *, search: str | None = None) -> dict[str, object]:
        """Read something."""
        spy.calls.append({"tool": "t_read", "connection": call.connection.label, "search": search})
        return {"connection": call.connection.label, "provider": call.provider.provider.value}

    @pms_tool(
        mcp,
        name="t_write",
        capability=Capability.RESERVATIONS_ADD_NOTE,
        api=lambda p: p.reservations,
    )
    async def t_write(call: ToolCall[Any], *, text: str, slow: bool = False) -> dict[str, object]:
        """Add a note."""
        spy.calls.append({"tool": "t_write", "connection": call.connection.label})
        if slow:
            await asyncio.sleep(30)
        return {"added": True}

    @pms_tool(
        mcp,
        name="guesty_t_only",
        capability=Capability.LISTINGS_READ,
        api=lambda p: p.listings,
        provider=Provider.GUESTY,
    )
    async def guesty_t_only(call: ToolCall[Any]) -> dict[str, object]:
        """Guesty only."""
        return {"ok": True}

    @pms_tool(mcp, name="t_calendar", capability=Capability.CALENDAR_READ, api=lambda p: p.calendar)
    async def t_calendar(call: ToolCall[Any]) -> dict[str, object]:
        """Needs a capability the fake provider lacks."""
        return {"ok": True}

    @pms_tool(mcp, name="t_fail", capability=Capability.LISTINGS_READ, api=lambda p: p.listings)
    async def t_fail(call: ToolCall[Any], *, kind: str) -> dict[str, object]:
        """Fail in a chosen way."""
        if kind == "crash":
            raise RuntimeError(SECRET_TEXT)
        if kind == "value":
            raise ValueError(SECRET_TEXT)
        raise PMS_ERRORS[kind]

    @pms_tool(mcp, name="t_slow", capability=Capability.LISTINGS_READ, api=lambda p: p.listings)
    async def t_slow(call: ToolCall[Any]) -> dict[str, object]:
        """Never answers in time."""
        await asyncio.sleep(30)
        return {}

    @pms_tool(mcp, name="t_deadline", capability=Capability.LISTINGS_READ, api=lambda p: p.listings)
    async def t_deadline(call: ToolCall[Any]) -> dict[str, object]:
        """Reports the time left."""
        return {"remaining": remaining_seconds()}

    @pms_tool(mcp, name="t_big", capability=Capability.LISTINGS_READ, api=lambda p: p.listings)
    async def t_big(call: ToolCall[Any], *, size: int) -> dict[str, object]:
        """Returns a lot of text."""
        return {"text": "x" * size}


def make_loader(spy: Spy, connection_ids: dict[str, tuple[str, Provider]]) -> Any:
    """A replacement for `registry.load_provider` that serves fake providers."""

    async def load(connection_id: str) -> PMSProvider:
        label, provider = connection_ids[connection_id]
        loaded = WriterProvider(label, provider, connection_id)
        original = loaded.aclose

        async def close() -> None:
            spy.closed += 1
            await original()

        loaded.aclose = close  # type: ignore[method-assign]
        return loaded

    return load
