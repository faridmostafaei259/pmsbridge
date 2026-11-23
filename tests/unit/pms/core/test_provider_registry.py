import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from guesty_fakes import FakeGuestyAuth
from pms_helpers import CLIENT_ID, make_connection

import pmsbridge.pms.guesty as guesty_package
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.pms.base import (
    Capability,
    ConnectionRef,
    PMSAuthError,
    PMSNotFoundError,
    PMSProvider,
    PMSValidationError,
    Provider,
)
from pmsbridge.pms.guesty.provider import (
    DOMAIN_MODULES,
    GuestyProvider,
    build_provider,
    discover_domains,
)
from pmsbridge.pms.registry import load_provider

REF = ConnectionRef("c" * 32, Provider.GUESTY, "Main")
CREDENTIALS = {"client_id": CLIENT_ID, "client_secret": "s"}


@pytest.fixture
def extra_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A folder searched for `pmsbridge.pms.guesty.<domain>` modules, cleaned up afterwards."""
    monkeypatch.setattr(guesty_package, "__path__", [*guesty_package.__path__, str(tmp_path)])
    yield tmp_path
    for name in DOMAIN_MODULES:
        sys.modules.pop(f"pmsbridge.pms.guesty.{name}", None)
        if hasattr(guesty_package, name):
            monkeypatch.delattr(guesty_package, name, raising=False)


def write_module(folder: Path, name: str, body: str) -> None:
    (folder / f"{name}.py").write_text(textwrap.dedent(body))


LISTINGS_MODULE = """
    from pmsbridge.pms.base import Capability

    CAPABILITIES = frozenset({Capability.LISTINGS_READ})

    class Api:
        def __init__(self, client):
            self.client = client

    def build(client):
        return Api(client)
"""


def test_with_no_domain_modules_the_provider_has_no_capabilities() -> None:
    provider = build_provider(REF, CREDENTIALS, None)
    assert provider.capabilities == frozenset()
    for attribute in (
        "listings",
        "content",
        "pricing",
        "reservations",
        "calendar",
        "guests",
        "inbox",
        "reviews",
        "owners",
        "payments",
        "tasks",
    ):
        assert getattr(provider, attribute) is None
    assert isinstance(provider, PMSProvider)
    assert provider.provider is Provider.GUESTY and provider.connection == REF


async def test_building_a_provider_never_calls_the_pms(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    build_provider(REF, CREDENTIALS, None)
    assert fake_auth.requests == 0 and mock_guesty.calls.call_count == 0


def test_existing_domain_modules_are_discovered(extra_modules: Path) -> None:
    write_module(extra_modules, "listings", LISTINGS_MODULE)
    write_module(
        extra_modules, "financials", LISTINGS_MODULE.replace("LISTINGS_READ", "PAYMENTS_READ")
    )
    provider = build_provider(REF, CREDENTIALS, "acct")
    assert isinstance(provider, GuestyProvider)
    assert provider.capabilities == {Capability.LISTINGS_READ, Capability.PAYMENTS_READ}
    assert provider.listings is not None and provider.payments is not None
    assert provider.reservations is None
    assert set(discover_domains()) == {"listings", "financials"}


def test_only_a_missing_domain_module_is_skipped(extra_modules: Path) -> None:
    write_module(extra_modules, "tasks", "import a_dependency_that_is_not_installed_anywhere\n")
    with pytest.raises(ModuleNotFoundError) as error:
        discover_domains()
    assert error.value.name == "a_dependency_that_is_not_installed_anywhere"


def test_a_broken_domain_module_fails_loudly(extra_modules: Path) -> None:
    write_module(extra_modules, "guests", "raise RuntimeError('broken')\n")
    with pytest.raises(RuntimeError):
        discover_domains()


def test_discovery_is_logged_once(extra_modules: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    write_module(extra_modules, "owners", LISTINGS_MODULE)
    discover_domains()
    discover_domains()
    messages = [r for r in caplog.records if "domains loaded" in r.getMessage()]
    assert len(messages) == 1 and messages[0].domains == ["owners"]


def test_every_documented_domain_has_a_slot() -> None:
    assert set(DOMAIN_MODULES) == {
        "listings",
        "reservations",
        "calendar",
        "guests",
        "inbox",
        "reviews",
        "owners",
        "financials",
        "tasks",
        "content",
        "pricing",
    }


# ---- registry ----


async def test_the_registry_builds_a_provider_for_a_connection(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Main office", account_id="acct-1")
    provider = await load_provider(connection_id)
    assert provider.provider is Provider.GUESTY
    assert provider.connection == ConnectionRef(connection_id, Provider.GUESTY, "Main office")
    assert fake_auth.requests == 0  # no token is requested just by building


async def test_an_unknown_connection_is_not_found() -> None:
    with pytest.raises(PMSNotFoundError):
        await load_provider("f" * 32)


@pytest.mark.parametrize("status", ["disabled", "needs_attention"])
async def test_an_unusable_connection_is_refused_with_a_clear_message(status: str) -> None:
    connection_id = await make_connection(status=status)
    with pytest.raises(PMSAuthError) as error:
        await load_provider(connection_id)
    assert CLIENT_ID not in error.value.message


async def test_a_connection_with_a_low_budget_is_still_usable() -> None:
    connection_id = await make_connection(status="token_budget_low")
    assert (await load_provider(connection_id)).provider is Provider.GUESTY


async def test_a_provider_without_an_adapter_yet_is_reported() -> None:
    connection_id = await make_connection("Hostaway one", provider="hostaway")
    with pytest.raises(PMSValidationError) as error:
        await load_provider(connection_id)
    assert "not available yet" in error.value.message


async def test_unreadable_credentials_are_reported_without_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    connection_id = await make_connection()
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("other"))
    clear_settings_cache()
    with pytest.raises(PMSAuthError) as error:
        await load_provider(connection_id)
    assert "enter them again" in error.value.message
    assert error.value.__cause__ is None
    assert CLIENT_ID not in caplog.text
