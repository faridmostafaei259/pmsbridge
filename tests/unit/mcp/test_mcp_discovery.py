"""Tool discovery follows the rule for discovery errors; the server logs what it loaded."""

import importlib.util
import logging
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer

import pmsbridge.mcp.tools as tools_package
import pmsbridge.mcp.tools.connections  # noqa: F401  (kept loaded while other modules are hidden)
from pmsbridge.app import create_app
from pmsbridge.mcp.server import TOOL_DOMAINS, build_server, load_tool_modules, tool_module_names

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "pmsbridge.mcp.tools."


@pytest.fixture
def extra_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The only folder searched for new `pmsbridge.mcp.tools.<name>` modules during the test.

    Real tool modules other than `connections` are hidden, so these tests do not depend on which
    domains exist. Everything is restored afterwards.
    """
    monkeypatch.setattr(tools_package, "__path__", [str(tmp_path)])
    for name in list(sys.modules):
        if name.startswith(PREFIX) and name != PREFIX + "connections":
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.delattr(tools_package, name.removeprefix(PREFIX), raising=False)
    importlib.invalidate_caches()
    before = set(vars(tools_package))
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith(PREFIX) and name != PREFIX + "connections":
            sys.modules.pop(name, None)
    for name in set(vars(tools_package)) - before:
        vars(tools_package).pop(name, None)


def write(folder: Path, name: str, body: str) -> None:
    (folder / f"{name}.py").write_text(textwrap.dedent(body))


GOOD = """
    def register(mcp):
        @mcp.tool()
        def {tool}() -> str:
            "A tool."
            return "ok"
"""


async def names(server: MCPServer) -> list[str]:
    return sorted(tool.name for tool in await server.list_tools())


async def test_a_module_that_does_not_exist_yet_is_skipped() -> None:
    server = MCPServer("test")
    assert load_tool_modules(server, [PREFIX + "not_a_real_domain", PREFIX + "connections"]) == [
        PREFIX + "connections"
    ]
    assert await names(server) == ["list_connections"]


async def test_modules_that_exist_are_loaded(extra_modules: Path) -> None:
    write(extra_modules, "listings", GOOD.format(tool="list_things"))
    write(extra_modules, "guesty_things", GOOD.format(tool="guesty_things"))
    server = MCPServer("test")
    loaded = load_tool_modules(server, tool_module_names())
    assert PREFIX + "listings" in loaded and PREFIX + "guesty_things" in loaded
    assert await names(server) == ["guesty_things", "list_connections", "list_things"]


async def test_a_missing_dependency_inside_a_module_is_raised(extra_modules: Path) -> None:
    write(extra_modules, "listings", "import a_dependency_that_is_not_installed\n")
    with pytest.raises(ModuleNotFoundError) as problem:
        load_tool_modules(MCPServer("test"), [PREFIX + "listings"])
    assert problem.value.name == "a_dependency_that_is_not_installed"


async def test_a_missing_submodule_of_a_module_is_raised(extra_modules: Path) -> None:
    write(extra_modules, "guests", "import pmsbridge.mcp.tools.guests_helpers\n")
    with pytest.raises(ModuleNotFoundError) as problem:
        load_tool_modules(MCPServer("test"), [PREFIX + "guests"])
    assert problem.value.name == PREFIX + "guests_helpers"


async def test_a_bug_in_a_module_is_raised(extra_modules: Path) -> None:
    write(extra_modules, "owners", "raise RuntimeError('broken module')\n")
    with pytest.raises(RuntimeError, match="broken module"):
        load_tool_modules(MCPServer("test"), [PREFIX + "owners"])


async def test_a_module_without_register_is_an_error(extra_modules: Path) -> None:
    write(extra_modules, "tasks", "x = 1\n")
    with pytest.raises(RuntimeError, match="does not define register"):
        load_tool_modules(MCPServer("test"), [PREFIX + "tasks"])


def test_provider_specific_modules_are_found_by_their_prefix(extra_modules: Path) -> None:
    write(extra_modules, "guesty_accounting", GOOD.format(tool="a"))
    write(extra_modules, "hostaway_extras", GOOD.format(tool="b"))
    write(extra_modules, "unrelated_thing", GOOD.format(tool="c"))
    found = tool_module_names()
    assert found[: len(TOOL_DOMAINS)] == [PREFIX + domain for domain in TOOL_DOMAINS]
    assert PREFIX + "guesty_accounting" in found and PREFIX + "hostaway_extras" in found
    assert PREFIX + "unrelated_thing" not in found


def test_the_server_logs_the_tool_modules_it_loaded(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="pmsbridge.mcp.server"):
        build_server()
    assert any(
        "tool modules loaded" in record.getMessage() and "connections" in record.getMessage()
        for record in caplog.records
    )


def test_the_app_mounts_the_endpoint(app: FastAPI) -> None:
    assert any(getattr(route, "path", None) == "/mcp" for route in app.routes)
    assert app.state.mcp_server is not None


def test_an_app_without_the_mcp_feature_has_no_endpoint() -> None:
    bare = create_app(("pmsbridge.core.web",))
    assert not any(getattr(route, "path", None) == "/mcp" for route in bare.routes)


# ---- the generated tool reference ----


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_tool_docs", ROOT / "scripts" / "gen_tool_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_generator()
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "tools.md")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def test_tool_docs_check_passes_after_generation(generator: ModuleType) -> None:
    assert generator.main(["--check"]) == 1
    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0


def test_tool_docs_check_fails_when_stale(generator: ModuleType) -> None:
    generator.main([])
    generator.OUTPUT.write_text(generator.OUTPUT.read_text() + "hand edit\n")
    assert generator.main(["--check"]) == 1


def test_committed_tool_docs_are_current() -> None:
    assert load_generator().main(["--check"]) == 0


def test_tool_docs_describe_each_tool() -> None:
    text = load_generator().render()
    assert text.startswith("<!-- Generated by scripts/gen_tool_docs.py")
    assert "## `list_connections`" in text and "**Read only.**" in text


def test_tool_docs_show_parameters_and_changes(extra_modules: Path) -> None:
    write(
        extra_modules,
        "listings",
        '''
        from typing import Any

        from pmsbridge.mcp.framework import ToolCall, pms_tool
        from pmsbridge.pms.base import Capability

        def register(mcp):
            @pms_tool(
                mcp,
                name="add_note",
                capability=Capability.RESERVATIONS_ADD_NOTE,
                api=lambda p: p.reservations,
            )
            async def add_note(call: ToolCall[Any], *, text: str, limit: int | None = None) -> str:
                """Add a note.

                Second paragraph | with a pipe.
                """
                return "ok"
        ''',
    )
    text = load_generator().render()
    assert "## `add_note`" in text and "**Changes data.**" in text
    assert "| `text` | string | yes |" in text
    assert "| `limit` | integer (optional) | no |" in text
    assert "| `connection` | string (optional) | no |" in text
    assert "Add a note.\n\nSecond paragraph | with a pipe." in text
