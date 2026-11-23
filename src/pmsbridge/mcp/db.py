"""Registers the MCP feature's tables (the audit log, defined in `pmsbridge.core.audit`)."""

from pmsbridge.core.audit import AuditLog

__all__ = ["AuditLog"]
