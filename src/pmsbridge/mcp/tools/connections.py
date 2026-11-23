"""The `list_connections` tool: which PMS connections this AI assistant may use."""

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from pmsbridge.mcp.context import ToolContext, allowed_connections
from pmsbridge.mcp.framework import account_tool


class ConnectionInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Pass this (or the label) as the connection argument of a tool.")
    label: str = Field(description="The name the company gave the connection.")
    provider: str = Field(description="The property management system: guesty or hostaway.")
    status: str = Field(
        description=(
            "active and token_budget_low connections can be used; needs_attention and disabled "
            "ones cannot until an administrator fixes them."
        )
    )
    writes_enabled: bool = Field(
        description="Whether an administrator has allowed tools that change data here."
    )


class ConnectionList(BaseModel):
    model_config = ConfigDict(frozen=True)

    connections: list[ConnectionInfo]


def register(mcp: MCPServer) -> None:
    @account_tool(mcp, name="list_connections", title="List connections")
    async def list_connections(ctx: ToolContext) -> ConnectionList:  # pyright: ignore[reportUnusedFunction]
        """List the property management connections this AI assistant may use.

        Call this first when you do not know which connection to use. Every other tool takes an
        optional `connection` argument (a label or id from this list); it can be left out when
        exactly one connection is available. Only connections the person allowed for this
        assistant are shown.
        """
        return ConnectionList(
            connections=[
                ConnectionInfo(
                    id=item.ref.connection_id,
                    label=item.ref.label,
                    provider=item.ref.provider.value,
                    status=item.status,
                    writes_enabled=item.writes_enabled,
                )
                for item in await allowed_connections(ctx)
            ]
        )
