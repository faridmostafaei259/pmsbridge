"""The provider registry: builds a `PMSProvider` for a connection id.

The connection and its credentials are loaded from the database and decrypted only in memory; the
provider shell wires the token manager and the HTTP client. A connection that is disabled or needs
attention is refused with a message an AI client can pass on. Each provider's module is imported
only when needed, and only a missing provider module is reported as "not available yet".
"""

import importlib
import logging

from pmsbridge.core.crypto import VaultError
from pmsbridge.core.storage import session
from pmsbridge.pms.base import (
    ConnectionRef,
    PMSAuthError,
    PMSNotFoundError,
    PMSProvider,
    PMSValidationError,
    Provider,
)
from pmsbridge.pms.db import PMSConnection, open_credentials

logger = logging.getLogger(__name__)

_UNUSABLE = {
    "disabled": "This connection is turned off.",
    "needs_attention": (
        "This connection needs attention: an administrator must check its credentials."
    ),
}


def provider_module(provider: Provider) -> str:
    return f"pmsbridge.pms.{provider.value}.provider"


async def load_provider(connection_id: str) -> PMSProvider:
    """The provider for one connection. Raises a `PMSError` the caller can show."""
    async with session() as db:
        row = await db.get(PMSConnection, connection_id)
    if row is None:
        raise PMSNotFoundError("That connection does not exist.")
    if row.status in _UNUSABLE:
        raise PMSAuthError(_UNUSABLE[row.status])
    try:
        credentials = open_credentials(row)
    except (VaultError, ValueError):
        logger.error("stored credentials could not be read", extra={"connection_id": row.id})
        raise PMSAuthError(
            "The stored credentials for this connection could not be read. An administrator "
            "must enter them again."
        ) from None
    provider = Provider(row.provider)
    module_name = provider_module(provider)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise PMSValidationError(f"{provider.value} is not available yet.") from None
    ref = ConnectionRef(connection_id=row.id, provider=provider, label=row.label)
    built: PMSProvider = module.build_provider(ref, credentials, row.guesty_account_id)
    return built
