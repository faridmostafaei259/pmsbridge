"""The Guesty provider shell.

It finds the domain modules that exist (`pmsbridge.pms.guesty.<domain>`), collects their
`CAPABILITIES` and builds each domain API with `build(client)`. A module that does not exist yet is
skipped; any other import problem is raised so that it fails loudly. With no domain modules the
provider has no capabilities.
"""

import importlib
import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Final, cast

from pmsbridge.pms.base import (
    CalendarAPI,
    Capability,
    ConnectionRef,
    GuestsAPI,
    InboxAPI,
    ListingContentAPI,
    ListingsAPI,
    OwnersAPI,
    PaymentsAPI,
    PricingAPI,
    Provider,
    ReservationsAPI,
    ReviewsAPI,
    TasksAPI,
)
from pmsbridge.pms.guesty.auth import GuestyCredentials, GuestyTokenManager
from pmsbridge.pms.guesty.client import GuestyClient

logger = logging.getLogger(__name__)

PACKAGE: Final = "pmsbridge.pms.guesty"
# Module name -> the provider attribute it fills.
DOMAIN_MODULES: Final[dict[str, str]] = {
    "listings": "listings",
    "reservations": "reservations",
    "calendar": "calendar",
    "guests": "guests",
    "inbox": "inbox",
    "reviews": "reviews",
    "owners": "owners",
    "financials": "payments",
    "tasks": "tasks",
    "content": "content",
    "pricing": "pricing",
}

_loaded: dict[str, ModuleType] | None = None


def discover_domains() -> dict[str, ModuleType]:
    """The domain modules that exist, by module name. Logged once, the first time."""
    global _loaded  # noqa: PLW0603
    if _loaded is None:
        found: dict[str, ModuleType] = {}
        for name in DOMAIN_MODULES:
            full_name = f"{PACKAGE}.{name}"
            try:
                found[name] = importlib.import_module(full_name)
            except ModuleNotFoundError as exc:
                if exc.name != full_name:
                    raise
        _loaded = found
        logger.info("guesty domains loaded", extra={"domains": sorted(found)})
    return _loaded


def reset_discovery() -> None:
    """Forget what was discovered (tests)."""
    global _loaded  # noqa: PLW0603
    _loaded = None


@dataclass(frozen=True)
class GuestyProvider:
    """One Guesty connection. Domain APIs are `None` for domains without a module."""

    connection: ConnectionRef
    capabilities: frozenset[Capability]
    _apis: dict[str, object]
    provider: Provider = Provider.GUESTY

    @property
    def listings(self) -> ListingsAPI | None:
        return cast(ListingsAPI | None, self._apis.get("listings"))

    @property
    def content(self) -> ListingContentAPI | None:
        return cast(ListingContentAPI | None, self._apis.get("content"))

    @property
    def pricing(self) -> PricingAPI | None:
        return cast(PricingAPI | None, self._apis.get("pricing"))

    @property
    def reservations(self) -> ReservationsAPI | None:
        return cast(ReservationsAPI | None, self._apis.get("reservations"))

    @property
    def calendar(self) -> CalendarAPI | None:
        return cast(CalendarAPI | None, self._apis.get("calendar"))

    @property
    def guests(self) -> GuestsAPI | None:
        return cast(GuestsAPI | None, self._apis.get("guests"))

    @property
    def inbox(self) -> InboxAPI | None:
        return cast(InboxAPI | None, self._apis.get("inbox"))

    @property
    def reviews(self) -> ReviewsAPI | None:
        return cast(ReviewsAPI | None, self._apis.get("reviews"))

    @property
    def owners(self) -> OwnersAPI | None:
        return cast(OwnersAPI | None, self._apis.get("owners"))

    @property
    def payments(self) -> PaymentsAPI | None:
        return cast(PaymentsAPI | None, self._apis.get("payments"))

    @property
    def tasks(self) -> TasksAPI | None:
        return cast(TasksAPI | None, self._apis.get("tasks"))

    async def aclose(self) -> None:
        """Nothing to release: HTTP clients and throttles are shared."""


def build_provider(
    connection: ConnectionRef, credentials: dict[str, str], account_id: str | None
) -> GuestyProvider:
    """Wire the token manager, client and every discovered domain for one connection."""
    manager = GuestyTokenManager(
        connection.connection_id, GuestyCredentials.from_mapping(credentials), account_id
    )
    client = GuestyClient(manager, connection.connection_id)
    capabilities: set[Capability] = set()
    apis: dict[str, object] = {}
    for name, module in discover_domains().items():
        capabilities |= cast(frozenset[Capability], module.CAPABILITIES)
        apis[DOMAIN_MODULES[name]] = module.build(client)
    return GuestyProvider(connection, frozenset(capabilities), apis)
