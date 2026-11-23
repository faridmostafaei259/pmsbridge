"""The audit log: one row for every tool call an AI assistant makes.

A row says who (person and assistant), which connection, which tool, how it ended and how long it
took. It never holds arguments, results, names, messages or tokens, so nothing in it is personal
data. Rows older than `AUDIT_RETENTION_DAYS` are deleted by `purge_expired`, which the MCP feature
runs at startup and then every few hours.

The table is registered with the other models through `pmsbridge.mcp.db`.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import CheckConstraint, Index, Integer, String, delete
from sqlalchemy.orm import Mapped, mapped_column

from pmsbridge.accounts.db import UtcDateTime
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import Base, write_transaction

logger = logging.getLogger(__name__)

STATUSES: Final = ("ok", "denied", "error", "timeout")
PURGE_INTERVAL_SECONDS: Final = 6 * 3600


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'denied', 'error', 'timeout')", name="status"),
        Index("ix_audit_log_time", "time"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time: Mapped[datetime] = mapped_column(UtcDateTime)
    # Plain values, not foreign keys: the record outlives a removed person or connection.
    user_id: Mapped[int] = mapped_column(Integer)
    client_id: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[str | None] = mapped_column(String(32), default=None)
    tool: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8))
    duration_ms: Mapped[int] = mapped_column(Integer)
    # The name of the error class or a short code (never the error text), or None when it worked.
    error_class: Mapped[str | None] = mapped_column(String(64), default=None)


async def record(
    *,
    user_id: int,
    client_id: str,
    connection_id: str | None,
    tool: str,
    status: str,
    duration_ms: int,
    error_class: str | None = None,
) -> None:
    """Write one audit row. A failure to write is logged and never reaches the caller."""
    if status not in STATUSES:
        raise ValueError(f"unknown audit status {status!r}")
    try:
        async with write_transaction() as db:
            db.add(
                AuditLog(
                    time=datetime.now(UTC),
                    user_id=user_id,
                    client_id=client_id[:64],
                    connection_id=connection_id,
                    tool=tool[:64],
                    status=status,
                    duration_ms=max(0, duration_ms),
                    error_class=error_class[:64] if error_class else None,
                )
            )
    except Exception:
        logger.exception("could not write an audit entry", extra={"tool": tool})


async def purge_expired(now: datetime | None = None) -> int:
    """Delete rows older than the retention period; returns how many were deleted."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=get_settings().audit_retention_days)
    async with write_transaction() as db:
        result = await db.execute(delete(AuditLog).where(AuditLog.time < cutoff))
        return int(getattr(result, "rowcount", 0) or 0)


async def purge_loop() -> None:
    """Purge now and then every `PURGE_INTERVAL_SECONDS`, until cancelled."""
    while True:
        try:
            removed = await purge_expired()
            if removed:
                logger.info("old audit entries deleted", extra={"count": removed})
        except Exception:
            logger.exception("audit log cleanup failed")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


def start_purge_task() -> "asyncio.Task[None]":
    return asyncio.create_task(purge_loop(), name="audit-purge")


async def stop_purge_task(task: "asyncio.Task[None]") -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
