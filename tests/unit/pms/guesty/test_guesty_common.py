"""The shared Guesty helpers: id quoting, safe readers and nothing imported across domains."""

from pathlib import Path

import pytest

from pmsbridge.pms.base import PMSValidationError
from pmsbridge.pms.guesty import common
from pmsbridge.pms.guesty import content as content_module
from pmsbridge.pms.guesty import listings as listings_module


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [("abc", "abc"), (" a/b ", "a%2Fb"), ("a b", "a%20b"), ("a?b", "a%3Fb")],
)
def test_quote_id(raw: str, quoted: str) -> None:
    assert common.quote_id(raw) == quoted


@pytest.mark.parametrize("bad", ["", "  ", "x" * 201])
def test_quote_id_refuses_bad_ids_with_a_neutral_message(bad: str) -> None:
    with pytest.raises(PMSValidationError) as caught:
        common.quote_id(bad)
    assert "listing" not in caught.value.message


def test_readers_return_none_for_the_wrong_type() -> None:
    assert common.as_dict({"a": 1}) == {"a": 1} and common.as_dict([1]) is None
    assert common.as_list([1]) == [1] and common.as_list({}) is None
    assert common.text_of("  hi ") == "hi" and common.text_of("  ") is None
    assert common.text_of(5) is None
    assert common.number_of(2) == 2 and common.number_of(True) is None
    assert common.number_of(float("nan")) is None and common.number_of("3") is None
    assert common.count_of(-1) is None and common.count_of(3.9) == 3
    assert common.flag_of(False) is False and common.flag_of(0) is None
    assert common.strings_of(["a", "", 3, " b "], limit=5) == ["a", "b"]
    assert common.strings_of(["a", "b", "c"], limit=2) == ["a", "b"]
    assert common.strings_of("a", limit=2) == []


def test_domain_modules_do_not_import_each_other() -> None:
    for module, other in ((listings_module, "content"), (content_module, "listings")):
        source = Path(module.__file__ or "").read_text()
        assert f"pms.guesty.{other}" not in source
        assert f"pms.guesty import {other}" not in source
