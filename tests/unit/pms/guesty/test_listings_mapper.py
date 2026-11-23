"""Mapping Guesty listing, detail and photo payloads to the canonical models (invented fixtures)."""

import logging
from decimal import Decimal

import pytest
from guesty_listing_helpers import fixture

from pmsbridge.pms.base import PMSValidationError
from pmsbridge.pms.guesty.common import decode_cursor, encode_cursor
from pmsbridge.pms.guesty.content import map_details, map_photo
from pmsbridge.pms.guesty.listings import map_listing
from pmsbridge.pms.models import UNKNOWN_NAME, DescriptionKind, ListingStatus


def test_a_full_listing_maps_every_field() -> None:
    listing = map_listing(fixture("listing_full.json"))
    assert listing is not None
    assert listing.id == "6a1f00000000000000000001"
    assert (listing.title, listing.nickname) == ("Harbour View Loft", "Loft")
    assert listing.status is ListingStatus.ACTIVE
    assert listing.property_type == "Apartment"
    assert listing.address is not None
    assert listing.address.line1 == "12 Example Quay"
    assert (listing.address.city, listing.address.country) == ("Testville", "Exampleland")
    assert (listing.address.latitude, listing.address.longitude) == (12.5, -45.25)
    assert listing.address.state is None and listing.address.postal_code is None
    assert (listing.bedrooms, listing.max_guests) == (2, 5)
    assert listing.bathrooms is None
    assert listing.timezone == "Europe/Paris"
    assert listing.base_price is not None
    assert listing.base_price.amount == Decimal("149.5") and listing.base_price.currency == "EUR"
    assert (listing.check_in_time, listing.check_out_time) == ("15:00", "11:00")
    assert listing.tags == ["harbour", "loft"]


def test_extras_hold_only_scalar_display_values_never_channel_data() -> None:
    listing = map_listing(fixture("listing_full.json"))
    assert listing is not None
    assert listing.extras == {
        "room_type": "Entire home/apartment",
        "address_full": "12 Example Quay, Testville, Exampleland",
        "is_listed": True,
        "is_virtual": False,
        "is_test": False,
        "min_nights": 2,
        "max_nights": 30,
        "cleaning_status": "clean",
    }
    dumped = listing.model_dump_json()
    for leaked in ("fakechannel", "fake-channel", "integrations", "owners", "cafe"):
        assert leaked not in dumped


def test_a_minimal_listing_maps_with_unknowns() -> None:
    listing = map_listing(fixture("listing_minimal.json"))
    assert listing is not None
    assert listing.title == UNKNOWN_NAME
    assert listing.status is ListingStatus.UNKNOWN
    assert listing.address is None and listing.base_price is None
    assert listing.bedrooms is None and listing.tags == [] and listing.extras == {}


def test_odd_values_become_none_instead_of_failing() -> None:
    listing = map_listing(fixture("listing_odd_values.json"))
    assert listing is not None
    assert listing.title == UNKNOWN_NAME
    assert listing.status is ListingStatus.UNKNOWN
    assert listing.address is not None and listing.address.line1 == "5 Sample Road"
    assert listing.address.latitude is None and listing.address.longitude is None
    assert listing.bedrooms is None
    assert listing.max_guests == 3
    assert listing.base_price is None
    assert listing.tags == ["one", "two"]
    assert listing.extras == {}


@pytest.mark.parametrize("raw", ["text", 42, None, [], {}, {"_id": ""}, {"_id": 7}, {"title": "x"}])
def test_an_item_without_a_usable_id_is_skipped(raw: object) -> None:
    assert map_listing(raw) is None


def test_a_skipped_item_is_logged_by_id_only(caplog: pytest.LogCaptureFixture) -> None:
    # An id longer than the canonical limit makes the mapping fail after the id was read.
    with caplog.at_level(logging.WARNING):
        assert map_listing({"_id": "x" * 500, "title": "Private name"}) is None
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.listing_id == "x" * 80  # type: ignore[attr-defined]
    for value in record.__dict__.values():
        assert "Private name" not in str(value)
    assert "Private name" not in caplog.text


def test_an_item_with_no_id_is_logged_without_any_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert map_listing({"title": "Private name"}) is None
    for value in caplog.records[-1].__dict__.values():
        assert "Private name" not in str(value)


def test_cursor_round_trip() -> None:
    for skip in (0, 1, 25, 99_999, 100_000):
        assert decode_cursor(encode_cursor(skip)) == skip
    assert decode_cursor(None) == 0
    assert "://" not in encode_cursor(50)


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "abc",
        "not base64 !!",
        "c2tpcD0x==",  # padding is not part of a cursor
        "c2tpcD0tMQ",  # skip=-1
        "c2tpcD0wMQ",  # skip=01 is not what this adapter writes
        "c2tpcD0xMDAwMDAx",  # skip=1000001
        "c2tpcD0x\n",
        "/w",
        "c2tpcD1hYg",  # skip=ab
        "c2tpcD0xJmxpbWl0PTE",  # extra parameters
        "aHR0cHM6Ly9ldmlsLmV4YW1wbGU",  # an address
    ],
)
def test_a_cursor_this_adapter_did_not_make_is_refused(cursor: str) -> None:
    with pytest.raises(PMSValidationError):
        decode_cursor(cursor)


def test_details_map_descriptions_by_key() -> None:
    details = map_details("6a1f00000000000000000001", fixture("details_full.json"))
    kinds = {(d.kind, d.text) for d in details.descriptions}
    assert (DescriptionKind.SUMMARY, "A bright loft by the harbour.") in kinds
    assert (DescriptionKind.SPACE, "Open plan living area and two bedrooms.") in kinds
    assert (DescriptionKind.INTERACTION, "Host is available by message.") in kinds
    assert (DescriptionKind.HOUSE_RULES, "No parties.") in kinds
    assert (DescriptionKind.NOTES, "Quiet building.") in kinds
    assert (DescriptionKind.OTHER, "Extra text under an unknown key.") in kinds
    assert "Staff note" not in details.model_dump_json()  # privateDescription is never read
    assert "not read" not in details.model_dump_json()


def test_details_amenities_checkin_and_custom_fields() -> None:
    details = map_details("6a1f00000000000000000001", fixture("details_full.json"))
    assert details.amenities == ["Wifi", "Kitchen", "Heating"]
    assert details.amenities_not_included == ["Pool"]
    assert details.check_in_instructions == (
        "Check-in method: door code. Alternative: lock box. Use the side entrance."
    )
    assert details.custom_fields == {"fieldA": "plain text", "fieldB": 12, "fieldC": True}
    assert details.house_rules is None and details.rooms == []


def test_minimal_details_do_not_fail() -> None:
    details = map_details("6a1f00000000000000000002", fixture("details_minimal.json"))
    assert [(d.kind, d.text) for d in details.descriptions] == [
        (DescriptionKind.SUMMARY, "Just a plain text.")
    ]
    assert details.amenities == [] and details.custom_fields == {}
    assert details.check_in_instructions is None


def test_photo_mapping() -> None:
    mapped = map_photo(fixture("photos.json")[0], 9)  # type: ignore[index]
    assert mapped is not None
    position, photo = mapped
    assert position == 2 and photo.position == 2
    assert photo.url == "https://images.example.test/c.jpg"
    assert photo.thumbnail_url == "https://images.example.test/c_t.jpg"
    assert photo.caption == "Third"
    assert photo.width is None and photo.room_name is None


def test_a_photo_without_an_index_uses_its_place_in_the_list() -> None:
    mapped = map_photo({"_id": "p1", "original": "https://images.example.test/x.jpg"}, 4)
    assert mapped is not None and mapped[1].position == 4


@pytest.mark.parametrize(
    "raw",
    [
        {"_id": "p1", "original": "javascript:alert(1)"},
        {"_id": "p1", "original": "ftp://images.example.test/x.jpg"},
        {"_id": "p1"},
        {"original": "https://images.example.test/x.jpg"},
        "text",
    ],
)
def test_an_unusable_photo_is_skipped(raw: object) -> None:
    assert map_photo(raw, 0) is None
