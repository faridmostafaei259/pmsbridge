"""Entry point for the in-container admin commands: `python -m pmsbridge.admin <feature> <action>`.

The host scripts run it with `docker compose exec app python -m pmsbridge.admin ...`. Every module
`pmsbridge.admin.<feature>` that exists is loaded and asked to add its commands through
`register(subparsers)`; each action sets a `handler` that does the work and returns the exit code.

Log records (audit lines) go to standard error as redacted JSON, so standard output holds only the
command's result: a link, a short confirmation or a list. This program never prints secrets: the
feature modules read them from a hidden prompt or standard input themselves.

Exit codes are the handler's own (0 done, 1 refused or unfinished, 2 bad input or configuration).
No feature, or an unknown one, prints the usage and exits with 2.
"""

import argparse
import importlib
import logging
import pkgutil
import sys
from collections.abc import Callable, Iterable
from types import ModuleType
from typing import cast

import pmsbridge.admin as package
from pmsbridge.core.logging import JsonFormatter, RedactionFilter
from pmsbridge.core.settings import SettingsError, get_settings

logger = logging.getLogger(__name__)

PROGRAM = "python -m pmsbridge.admin"
PACKAGE = package.__name__


Importer = Callable[[str], ModuleType]


def feature_names() -> list[str]:
    """Names of the modules in this package, other than the entry point itself."""
    return sorted(
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    )


def discover(
    names: Iterable[str] | None = None, import_module: Importer = importlib.import_module
) -> list[ModuleType]:
    """Import the feature modules.

    A module that does not exist is skipped, but only when the import error names exactly that
    module. Any other import error, including a missing dependency of an existing module, is raised.
    """
    modules: list[ModuleType] = []
    for name in feature_names() if names is None else names:
        full_name = f"{PACKAGE}.{name}"
        try:
            modules.append(import_module(full_name))
        except ModuleNotFoundError as problem:
            if problem.name != full_name:
                raise
    return modules


def build_parser(modules: Iterable[ModuleType]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="pmsbridge administration commands (run inside the app container).",
    )
    subparsers = parser.add_subparsers(dest="feature", required=True, metavar="<feature>")
    for module in modules:
        register = cast(
            "Callable[[argparse._SubParsersAction[argparse.ArgumentParser]], None] | None",  # pyright: ignore[reportPrivateUsage]
            getattr(module, "register", None),
        )
        if not callable(register):
            raise TypeError(f"{module.__name__} does not define register(subparsers)")
        register(subparsers)
    return parser


def configure_logging() -> None:
    """JSON audit lines on standard error, redacted, at the configured level."""
    try:
        level = get_settings().log_level
    except SettingsError:
        # The command reports a settings problem itself, in plain words.
        level = "INFO"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _exit_code(raised: SystemExit) -> int:
    code = raised.code
    if code is None:
        return 0
    return code if isinstance(code, int) else 2


def main(argv: list[str] | None = None, modules: Iterable[ModuleType] | None = None) -> int:
    configure_logging()
    found = discover() if modules is None else list(modules)
    parser = build_parser(found)
    try:
        args = parser.parse_args(argv)
    except SystemExit as raised:  # argparse: --help (0) or a usage error (2)
        return _exit_code(raised)
    handler = cast("Callable[[argparse.Namespace], int] | None", getattr(args, "handler", None))
    if handler is None:
        parser.print_usage(sys.stderr)
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
