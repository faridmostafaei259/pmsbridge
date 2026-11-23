"""Payment tools: read the payments of one reservation.

Read-only and works on any connection whose provider supports it. It returns the shared payment
model only: amount, status, a plain method word and the due and paid times. Card, bank and payment
method details are never part of it.
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from pmsbridge.mcp.framework import ToolCall, pms_tool
from pmsbridge.pms.base import Capability, PaymentsAPI
from pmsbridge.pms.models import Payment


class ReservationPayments(BaseModel):
    model_config = ConfigDict(frozen=True)

    reservation_id: str = Field(description="The reservation the payments belong to.")
    payments: list[Payment] = Field(description="The payments of the reservation.")


def register(mcp: MCPServer) -> None:
    @pms_tool(
        mcp,
        name="list_reservation_payments",
        capability=Capability.PAYMENTS_READ,
        api=lambda p: p.payments,
        title="List the payments of a reservation",
    )
    async def list_reservation_payments(  # pyright: ignore[reportUnusedFunction]
        call: ToolCall[PaymentsAPI],
        *,
        reservation_id: Annotated[
            str,
            Field(
                description="The reservation id, from list_reservations or get_reservation.",
                max_length=200,
            ),
        ],
    ) -> ReservationPayments:
        """List the payments recorded for one reservation.

        Returns each payment with its amount and currency, status (pending, paid, failed, refunded,
        partially refunded, cancelled or unknown), a plain method word when known, and the times it
        is due and was paid. Card numbers, bank details and payment method ids are never shown.
        Fails with a clear message if the reservation does not exist.
        """
        payments = await call.api.list_reservation_payments(reservation_id)
        return ReservationPayments(reservation_id=reservation_id.strip(), payments=payments)
