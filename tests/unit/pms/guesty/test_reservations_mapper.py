"""The Guesty reservation mapper: every field, every status, notes and the things never read."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from guesty_reservation_helpers import DOOR_CODE, GUEST_NOTE, STAFF_NAME, fixture

from pmsbridge.pms.guesty.reservations import (
    STATUS_FROM_GUESTY,
    STATUS_TO_GUESTY,
    map_reservation,
    notes_text,
)
from pmsbridge.pms.models import ReservationStatus


def first_result(name: str = "search_page.json") -> dict[str, Any]:
    body: Any = fixture(name)
    return body["results"][0]  # type: ignore[no-any-return]


def test_a_full_search_result() -> None:
    mapped = map_reservation(first_result())
    assert mapped is not None
    assert mapped.id == "6a2000000000000000000001"
    assert mapped.listing_id == "6a1f00000000000000000001"
    assert mapped.status is ReservationStatus.CONFIRMED
    assert mapped.channel == "airbnb2" and mapped.confirmation_code == "TST-1001"
    assert mapped.guest_id == "6a2000000000000000000a01"
    assert (mapped.check_in, mapped.check_out, mapped.nights) == (
        date(2027, 3, 10),
        date(2027, 3, 14),
        4,
    )
    assert mapped.guests_count == 3
    assert mapped.created_at == datetime(2027, 1, 5, 9, 30, tzinfo=UTC)
    assert mapped.guest is None and mapped.total_price is None and mapped.balance_due is None
    assert mapped.extras == {
        "pms_status": "confirmed",
        "source": "airbnb",
        "guest_stay_status": "not_set",
    }


def test_a_minimal_result() -> None:
    mapped = map_reservation(
        {
            "reservationId": "r1",
            "checkInDateLocalized": "2027-01-01",
            "checkOutDateLocalized": "2027-01-01",
            "stay": [{"listingId": "l1"}],
        }
    )
    assert mapped is not None
    assert mapped.status is ReservationStatus.UNKNOWN and mapped.nights == 0
    assert mapped.channel is None and mapped.guest_id is None and mapped.guests_count is None
    assert mapped.created_at is None and mapped.notes is None and mapped.extras == {}


def test_a_by_ids_result_uses_its_own_key_names() -> None:
    body: Any = fixture("by_ids.json")
    mapped = map_reservation(body[0])
    assert mapped is not None
    assert mapped.id == "6a2000000000000000000001"
    assert mapped.listing_id == "6a1f00000000000000000001"  # from stay[0].unitId
    assert mapped.guest_id == "6a2000000000000000000a01"  # from bookerId
    assert mapped.channel == "airbnb2"  # from platform


def test_a_multi_stay_reservation_uses_the_first_listing_and_the_whole_span() -> None:
    mapped = map_reservation(fixture("multi_stay.json"))
    assert mapped is not None
    assert mapped.listing_id == "6a1f00000000000000000005"
    assert (mapped.check_in, mapped.check_out, mapped.nights) == (
        date(2027, 8, 1),
        date(2027, 8, 9),
        8,
    )
    assert mapped.guests_count == 3  # the largest party


def test_missing_fields_skip_the_item_and_log_only_the_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")
    assert map_reservation("text") is None
    assert map_reservation({"status": "confirmed"}) is None
    no_dates = {"reservationId": "r-nodates", "stay": [{"listingId": "l1"}]}
    assert map_reservation(no_dates) is None
    no_listing = {"reservationId": "r-nolisting", "checkInDateLocalized": "2027-01-01"}
    assert map_reservation(no_listing) is None
    backwards = {
        "reservationId": "r-backwards",
        "checkInDateLocalized": "2027-02-10",
        "checkOutDateLocalized": "2027-02-05",
        "stay": [{"listingId": "l1"}],
        "notes": {"guest": GUEST_NOTE},
    }
    assert map_reservation(backwards) is None
    logged = [record for record in caplog.records if record.name.endswith("reservations")]
    assert len(logged) == 5
    for record in logged:
        assert GUEST_NOTE not in record.getMessage() and GUEST_NOTE not in str(record.__dict__)
    assert {getattr(record, "reservation_id", None) for record in logged} >= {
        "r-nodates",
        "r-nolisting",
        "r-backwards",
    }


@pytest.mark.parametrize(
    ("guesty", "canonical"),
    [
        ("inquiry", ReservationStatus.INQUIRY),
        ("awaiting_payment", ReservationStatus.PENDING),
        ("reserved", ReservationStatus.PENDING),
        ("confirmed", ReservationStatus.CONFIRMED),
        ("checked_in", ReservationStatus.CHECKED_IN),
        ("checked_out", ReservationStatus.CHECKED_OUT),
        ("completed", ReservationStatus.CHECKED_OUT),
        ("canceled", ReservationStatus.CANCELLED),
        ("declined", ReservationStatus.DECLINED),
        ("expired", ReservationStatus.EXPIRED),
        ("closed", ReservationStatus.UNKNOWN),
        ("alteration", ReservationStatus.UNKNOWN),
        ("something_new", ReservationStatus.UNKNOWN),
        ("CONFIRMED", ReservationStatus.CONFIRMED),
    ],
)
def test_every_status(guesty: str, canonical: ReservationStatus) -> None:
    mapped = map_reservation(
        {
            "reservationId": "r1",
            "status": guesty,
            "checkInDateLocalized": "2027-01-01",
            "checkOutDateLocalized": "2027-01-02",
            "stay": [{"listingId": "l1"}],
        }
    )
    assert mapped is not None and mapped.status is canonical
    assert mapped.extras["pms_status"] == guesty


def test_every_canonical_status_has_a_reverse_mapping() -> None:
    assert set(STATUS_TO_GUESTY) == set(ReservationStatus)
    for canonical, names in STATUS_TO_GUESTY.items():
        for name in names:
            expected = STATUS_FROM_GUESTY.get(name, ReservationStatus.UNKNOWN)
            assert expected is canonical


def test_notes_are_labelled_and_never_include_the_door_code() -> None:
    text = notes_text(first_result()["notes"])
    assert text == (
        "Guest note: Arriving late, after nine.\n"
        "Special requests: Extra towels please.\n"
        "Staff note: Repeat guest.\n"
        "Cleaning note: Deep clean before arrival."
    )
    mapped = map_reservation(first_result())
    assert mapped is not None
    shown = mapped.model_dump_json()
    assert DOOR_CODE not in shown and STAFF_NAME not in shown and "keyCode" not in shown
    only_secret = {"keyCode": DOOR_CODE, "doneBy": STAFF_NAME}
    assert notes_text(only_secret) is None
    assert notes_text({}) is None and notes_text("text") is None and notes_text(None) is None


def test_a_door_code_at_the_top_level_is_never_read() -> None:
    body: Any = fixture("by_ids.json")
    mapped = map_reservation(body[0])
    assert mapped is not None
    assert DOOR_CODE not in mapped.model_dump_json()
    assert mapped.notes == "Guest note: Arriving late, after nine."


def test_extras_hold_only_scalars_and_no_channel_or_payment_data() -> None:
    mapped = map_reservation(first_result())
    assert mapped is not None
    shown = mapped.model_dump_json()
    for leaked in ("EXT-INVENTED", "merchantOfRecord", "isUsingVCC", "vcc", "moneyId"):
        assert leaked not in shown
    assert all(isinstance(v, str | int | float | bool) for v in mapped.extras.values())


def test_odd_values_are_tolerated() -> None:
    mapped = map_reservation(
        {
            "reservationId": "r1",
            "status": 7,
            "checkInDateLocalized": "2027-01-01",
            "checkOutDateLocalized": "2027-01-03",
            "createdAt": "not a date",
            "channel": ["x"],
            "stay": [{"listingId": "l1", "guestsCount": -2}, "bad", {"guestsCount": 5}],
            "notes": {"guest": 12, "other": "  "},
        }
    )
    assert mapped is not None
    assert mapped.status is ReservationStatus.UNKNOWN and mapped.created_at is None
    assert mapped.channel is None and mapped.guests_count == 5 and mapped.notes is None


def test_a_created_time_without_an_offset_is_read_as_utc_because_guesty_documents_utc() -> None:
    mapped = map_reservation(
        {
            "reservationId": "r1",
            "createdAt": "2027-01-05T09:30:00",
            "checkInDateLocalized": "2027-01-01",
            "checkOutDateLocalized": "2027-01-03",
            "stay": [{"listingId": "l1"}],
        }
    )
    assert mapped is not None and mapped.created_at == datetime(2027, 1, 5, 9, 30, tzinfo=UTC)
