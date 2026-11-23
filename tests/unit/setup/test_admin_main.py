"""The in-container admin dispatcher: discovery, dispatch, exit codes and real subcommands."""

import argparse
import json
import logging
from types import ModuleType

import pytest

from pmsbridge.admin import __main__ as admin_main
from pmsbridge.core import storage
from pmsbridge.core.settings import get_settings

FEATURES = ["connections", "keys", "oauth", "users"]


def fake_feature(name: str, code: int = 0) -> ModuleType:
    module = ModuleType(f"pmsbridge.admin.{name}")

    def run(_args: argparse.Namespace) -> int:
        return code

    def register(
        subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        subparsers.add_parser(name).set_defaults(handler=run)

    module.register = register  # pyright: ignore[reportAttributeAccessIssue]
    return module


def test_discovery_finds_the_feature_modules_and_not_the_entry_point() -> None:
    assert admin_main.feature_names() == FEATURES
    modules = admin_main.discover()
    assert sorted(module.__name__.rsplit(".", 1)[1] for module in modules) == FEATURES


def test_discovery_skips_only_a_module_that_does_not_exist() -> None:
    def importer(name: str) -> ModuleType:
        if name == "pmsbridge.admin.later":
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return fake_feature(name.rsplit(".", 1)[1])

    found = admin_main.discover(["first", "later", "second"], importer)
    assert [module.__name__ for module in found] == [
        "pmsbridge.admin.first",
        "pmsbridge.admin.second",
    ]


def test_discovery_raises_when_an_existing_module_cannot_import_something() -> None:
    def importer(name: str) -> ModuleType:
        raise ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency")

    with pytest.raises(ModuleNotFoundError, match="missing_dependency"):
        admin_main.discover(["broken"], importer)


def test_discovery_raises_other_import_errors() -> None:
    def importer(name: str) -> ModuleType:
        raise ImportError("cannot import name 'x'", name=name)

    with pytest.raises(ImportError, match="cannot import"):
        admin_main.discover(["broken"], importer)


def test_a_module_without_register_is_an_error() -> None:
    with pytest.raises(TypeError, match="does not define register"):
        admin_main.build_parser([ModuleType("pmsbridge.admin.empty")])


def test_help_lists_every_feature(capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_main.main(["--help"]) == 0
    out = capsys.readouterr().out
    for feature in FEATURES:
        assert feature in out


def test_no_feature_prints_usage_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_main.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err


def test_an_unknown_feature_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_main.main(["nonsense"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_a_missing_action_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_main.main(["users"]) == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize("code", [0, 1, 2])
def test_the_exit_code_is_the_handlers(code: int) -> None:
    assert admin_main.main(["one"], [fake_feature("one", code)]) == code


def test_logging_goes_to_standard_error_as_redacted_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    admin_main.configure_logging()
    logging.getLogger("pmsbridge.admin.test").info("done password=hunter2-example")
    captured = capsys.readouterr()
    assert captured.out == ""
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["message"].startswith("done")
    assert "hunter2-example" not in captured.err


def test_users_list_runs_against_an_empty_database(capsys: pytest.CaptureFixture[str]) -> None:
    storage.migrate(get_settings())
    capsys.readouterr()
    assert admin_main.main(["users", "list"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "There are no users yet.\n"
    assert "Traceback" not in captured.err


def test_create_admin_prints_only_the_setup_link(capsys: pytest.CaptureFixture[str]) -> None:
    storage.migrate(get_settings())
    capsys.readouterr()
    assert admin_main.main(["users", "create-admin", "--email", "owner@example.test"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("http://localhost:8000/setup?token=")
    assert len(captured.out.strip().splitlines()) == 1
    assert "owner@example.test" not in captured.err
