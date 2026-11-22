"""A log call must never break the code that makes it, and must never print an unmasked record."""

import logging

import pytest

from pmsbridge.core.logging import configure_logging
from pmsbridge.core.settings import Settings

pytestmark = pytest.mark.usefixtures("restore_logging")


class _FailsToRender:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __str__(self) -> str:
        raise self._error

    __repr__ = __str__


@pytest.mark.parametrize(
    "error",
    [TypeError("bad"), ValueError("bad"), KeyError("bad"), IndexError("bad")],
    ids=lambda error: type(error).__name__,
)
def test_an_argument_that_cannot_be_rendered_does_not_break_the_caller(
    secure_settings: Settings, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    configure_logging(secure_settings)
    logging.getLogger("security.test").info("value %s", _FailsToRender(error))
    assert "unformattable" in capsys.readouterr().out


@pytest.mark.xfail(
    strict=True,
    raises=RuntimeError,
    reason="open audit finding: the filter contains only four exception types",
)
def test_gap_any_error_while_rendering_an_argument_is_contained(
    secure_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(secure_settings)
    # Raised by user code inside __str__. Today it escapes from the log call into the caller
    # (and its message is not masked at that point).
    logging.getLogger("security.test").info("value %s", _FailsToRender(RuntimeError("x")))
    assert "unformattable" in capsys.readouterr().out
